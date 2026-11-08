"""Command line entry point: run, chat, doctor, models, agents, config."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .agents import describe_roster
from .config import CANDIDATE_ENDPOINTS, Config, load_config
from .effort import resolve as resolve_effort
from .hardware import probe, recommend
from .scale import adjust as adjust_effort, capacity_for, describe as describe_capacity
from .orchestrator import RunResult, Team
from .providers import LocalProvider, ProviderError, discover, open_provider
from .session import SessionLog
from .ui import UI
from .workspace import Workspace

HELP = """\
OffGrid - a free, offline, multi-agent coding team on your own machine.

  offgrid run "add a /health endpoint to app.py"    one-shot team run
  offgrid chat                                      interactive session
  offgrid doctor                                    check the local model setup
  offgrid models                                    list models on the server
  offgrid agents                                    show the team roster
  offgrid memory [list|search|add|forget|review|reject|forgive|rejected] what it remembers
  offgrid library [list|search|verify|index]        offline reference library
  offgrid council [members|ping|ask]                 independent advisors
  offgrid hardware                                   what this machine can do
  offgrid fleet [list|add|remove|ping|scan|serve]    use your other devices' power
  offgrid security [review|map|watch|history|plan|encrypt|decrypt]   reviews, changes, encryption
  offgrid trust                                     what it promises and what it may do
  offgrid protect [--kit|--check|--backup PATH]     whose side it is on, and a household kit
  offgrid lexicon [--json] [--strict]               the merged meaning vocabulary
  offgrid distribute [--wizard|--build|--list|--to]  build or copy a portable app

Offline by default. `--council` consults other models when this one is stuck;
`--council-public` additionally allows keyless public ones, which sends your
question off this machine.

No account, no API key, no internet. Everything runs against a model server on
localhost (llama.cpp, Ollama, LM Studio, vLLM).
"""


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--endpoint", help="OpenAI-compatible base URL, e.g. http://127.0.0.1:8080/v1")
    parser.add_argument("--model", help="model name to request (default: first the server reports)")
    parser.add_argument("--workspace", default=".", help="project directory to work in (default: .)")
    parser.add_argument("--auto", action="store_true", help="run shell commands without asking (still blocks the dangerous ones)")
    parser.add_argument("--no-shell", action="store_true", help="forbid the agents from running any shell command")
    parser.add_argument("--offline", action="store_true", help="hard offline: refuse network tools and package installs")
    parser.add_argument("--allow-remote", action="store_true", help="allow a model endpoint that is not on localhost")
    parser.add_argument("--parallel", type=int, help="run independent plan steps concurrently (default 1)")
    parser.add_argument(
        "--effort",
        choices=["best", "balanced", "fast"],
        help="how hard to work before answering: best (default) > balanced > fast",
    )
    parser.add_argument("--max-turns", type=int, help="max tool turns per agent (default 12)")
    parser.add_argument("--temperature", type=float, help="sampling temperature (default 0.2)")
    parser.add_argument("--verify", default="", help="command that proves the work (default: auto-detect)")
    parser.add_argument("--no-color", action="store_true", help="plain text output")
    parser.add_argument("-q", "--quiet", action="store_true", help="only errors")
    parser.add_argument("-v", "--verbose", action="store_true", help="show every tool call")
    parser.add_argument("--no-memory", action="store_true", help="do not read or write long-term memory")
    parser.add_argument("--council", action="store_true", help="consult a council of independent models when stuck")
    parser.add_argument(
        "--council-public",
        action="store_true",
        help="also allow keyless PUBLIC council members (your question leaves this machine)",
    )
    parser.add_argument(
        "--council-members",
        default="",
        help="comma list: local, local-model:NAME, pollinations, url:https://host/v1#model",
    )
    parser.add_argument(
        "--no-council-auto",
        action="store_true",
        help="do not consult the council automatically when an agent admits ignorance",
    )
    parser.add_argument("--library-dir", default="", help="offline library folder (default: ./offgrid-library)")
    parser.add_argument(
        "--fleet",
        action="store_true",
        help="also use other devices on this network running `offgrid fleet serve`",
    )
    parser.add_argument(
        "--no-scale",
        action="store_true",
        help="ignore measured hardware and use the effort profile exactly as written",
    )
    parser.add_argument(
        "--no-review",
        action="store_true",
        help="do not ask about new lessons at the end; they stay pending",
    )
    parser.add_argument(
        "--no-security-review",
        action="store_true",
        help="skip the automatic security review of changed files",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="offgrid",
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"offgrid {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="one-shot team run")
    run.add_argument("task", nargs="+", help="what you want built or fixed")
    run.add_argument("--json", action="store_true", help="machine-readable result on stdout")
    add_common(run)

    chat = subparsers.add_parser("chat", help="interactive REPL")
    add_common(chat)

    doctor = subparsers.add_parser("doctor", help="check the local environment and model server")
    doctor.add_argument(
        "--sign-key",
        default="",
        metavar="PATH",
        help="key file for verifying a signed backup manifest",
    )
    add_common(doctor)

    models = subparsers.add_parser("models", help="list models on the local server")
    add_common(models)

    agents = subparsers.add_parser("agents", help="show the team roster")
    add_common(agents)

    memory = subparsers.add_parser("memory", help="inspect or edit long-term memory")
    memory.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=[
            "list",
            "search",
            "add",
            "forget",
            "stats",
            "review",
            "accept",
            "reject",
            "forgive",
            "edit",
            "rejected",
        ],
    )
    memory.add_argument("terms", nargs="*")
    add_common(memory)

    lexicon = subparsers.add_parser(
        "lexicon", help="show the merged concept vocabulary, its source and warnings"
    )
    lexicon.add_argument("--json", action="store_true", help="machine-readable output")
    lexicon.add_argument(
        "--strict", action="store_true", help="exit 1 if the project lexicon has warnings or conflicts"
    )
    add_common(lexicon)

    library = subparsers.add_parser("library", help="inspect the offline document library")
    library.add_argument("action", nargs="?", default="list", choices=["list", "search", "verify", "index"])
    library.add_argument("terms", nargs="*")
    add_common(library)

    council = subparsers.add_parser("council", help="inspect or query the council of advisors")
    council.add_argument("action", nargs="?", default="members", choices=["members", "ping", "ask"])
    council.add_argument("terms", nargs="*")
    add_common(council)

    config = subparsers.add_parser("config", help="print the resolved configuration")
    add_common(config)

    hardware = subparsers.add_parser("hardware", help="what this machine can do, and what it will run")
    hardware.add_argument("--refresh", action="store_true", help="re-measure instead of using the cached result")
    hardware.add_argument("--json", action="store_true", help="machine-readable report")
    add_common(hardware)

    fleet = subparsers.add_parser("fleet", help="borrow computing power from your other devices")
    fleet.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove", "ping", "scan", "serve"])
    fleet.add_argument("terms", nargs="*", help="add/remove: NAME URL [TOKEN]")
    fleet.add_argument("--port", type=int, default=0, help="port to serve on (default 8776)")
    fleet.add_argument("--token", default="", help="shared token for add/scan/serve (serve defaults to a generated one)")
    fleet.add_argument("--yes", action="store_true", help="scan: register every discovered peer without prompting")
    fleet.add_argument(
        "--json", action="store_true", help="scan: machine-readable discoveries and registration outcomes"
    )
    add_common(fleet)

    security = subparsers.add_parser("security", help="security reviews, connection map, encryption")
    security.add_argument(
        "action",
        nargs="?",
        default="review",
        choices=["review", "map", "watch", "history", "plan", "encrypt", "decrypt"],
    )
    security.add_argument("terms", nargs="*", help="path to encrypt or decrypt")
    security.add_argument("--changed", action="store_true", help="review only changed files")
    security.add_argument("--save", action="store_true", help="with watch: accept the current view as the new baseline")
    security.add_argument(
        "--review",
        "--reviews",
        dest="watch_reviews",
        action="store_true",
        help="with watch: re-review the workspace instead of the connection table",
    )
    security.add_argument(
        "--interval",
        type=float,
        default=None,
        help="with watch --review: seconds between workspace reviews (default 300)",
    )
    security.add_argument(
        "--once",
        action="store_true",
        help="with watch --review: run one review and exit",
    )
    security.add_argument(
        "--install-timer",
        action="store_true",
        help="with watch --review: install and enable a systemd user timer",
    )
    security.add_argument(
        "--remove-timer",
        action="store_true",
        help="with watch: disable and remove the workspace's systemd user timer",
    )
    security.add_argument(
        "--install-service",
        action="store_true",
        help="with watch --review: install and start a continuous systemd user service",
    )
    security.add_argument(
        "--remove-service",
        action="store_true",
        help="with watch: disable and remove the workspace's continuous service",
    )
    security.add_argument(
        "--service-file",
        "--write-service",
        dest="service_file",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help=(
            "with watch --review: write a continuous service file (default: "
            "$XDG_CONFIG_HOME/systemd/user) without enabling it"
        ),
    )
    security.add_argument("--json", action="store_true", help="machine-readable output")
    security.add_argument("--out", default="", help="where to restore a decrypted file or folder")
    security.add_argument(
        "--passphrase-env",
        default="",
        help="read the passphrase from this environment variable (never pass it as an argument)",
    )
    add_common(security)

    trust = subparsers.add_parser("trust", help="what this software promises, and what it may do")
    add_common(trust)

    protect = subparsers.add_parser(
        "protect", help="whose side this software is on, and the protections in place"
    )
    protect_mode = protect.add_mutually_exclusive_group()
    protect_mode.add_argument(
        "--kit", action="store_true", help="write a household continuity checklist"
    )
    protect_mode.add_argument(
        "--check",
        action="store_true",
        help=(
            "check continuity readiness: kit, freshness, SHA-256 verified "
            "(and optionally signed) backups, and local model"
        ),
    )
    protect.add_argument(
        "--out",
        default="",
        help="kit path to write, check, or use as the backup source (default: .offgrid/continuity.md)",
    )
    protect.add_argument(
        "--force", action="store_true", help="overwrite an existing kit or backup files"
    )
    protect.add_argument(
        "--backup",
        action="append",
        default=[],
        help=(
            "copy the kit and local model with a SHA256SUMS manifest to this existing "
            "off-workspace directory; with --check: verify this backup location instead "
            "(repeatable)"
        ),
    )
    protect.add_argument(
        "--sign-key",
        default="",
        metavar="PATH",
        help=(
            "HMAC-SHA256 key file for signing or verifying a backup manifest; "
            "created with mode 0600 if missing during backup, never during check"
        ),
    )
    protect.add_argument(
        "--max-age-days",
        type=float,
        default=30.0,
        help="with --check: maximum age of the continuity kit in days (default 30)",
    )
    protect.add_argument("--json", action="store_true", help="machine-readable output")
    add_common(protect)

    distribute = subparsers.add_parser(
        "distribute", help="build a portable app, or install it on a mounted device"
    )
    distribute.add_argument(
        "--wizard", action="store_true", help="interactive build and install wizard"
    )
    distribute.add_argument(
        "--build",
        nargs="?",
        const="",
        default=None,
        help=f"build a portable zip and .pyz (default: dist/offgrid-{__version__}-portable.zip)",
    )
    distribute.add_argument("--with-tests", action="store_true", help="include the test suite in the bundle")
    distribute.add_argument(
        "--list",
        dest="list_devices",
        action="store_true",
        help="list mounted directories that may be removable devices",
    )
    distribute.add_argument(
        "--to",
        "--flash",
        dest="to",
        default="",
        help="install into this mounted device directory (not a raw block device)",
    )
    distribute.add_argument(
        "--bundle", default="", help="bundle zip or pyz to install (default: build it, or find dist/)"
    )
    distribute.add_argument("--force", action="store_true", help="replace an existing build or install")
    distribute.add_argument("--yes", action="store_true", help="do not ask before copying to a device")
    distribute.add_argument("--dry-run", action="store_true", help="show what would be copied, without writing")
    distribute.add_argument("--json", action="store_true", help="machine-readable output")
    add_common(distribute)

    return parser


def overrides_from(args: argparse.Namespace) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if getattr(args, "endpoint", None):
        out["endpoint"] = args.endpoint
    if getattr(args, "model", None):
        out["model"] = args.model
    if getattr(args, "workspace", None):
        out["workspace"] = args.workspace
    if getattr(args, "auto", False):
        out["command_policy"] = "auto"
    if getattr(args, "no_shell", False):
        out["command_policy"] = "deny"
    if getattr(args, "offline", False):
        out["offline"] = True
        out["network"] = "deny"
    if getattr(args, "allow_remote", False):
        out["allow_remote"] = True
    if getattr(args, "parallel", None) is not None:
        out["parallel"] = max(1, args.parallel)
    if getattr(args, "effort", None):
        out["effort"] = args.effort
    if getattr(args, "max_turns", None) is not None:
        out["max_turns"] = max(1, args.max_turns)
    if getattr(args, "temperature", None) is not None:
        out["temperature"] = args.temperature
    if getattr(args, "verify", ""):
        out["verify_command"] = args.verify
    if getattr(args, "no_color", False):
        out["color"] = False
    if getattr(args, "quiet", False):
        out["verbose"] = 0
    if getattr(args, "verbose", False):
        out["verbose"] = 2
    if getattr(args, "json", False):
        out["color"] = False
        out["verbose"] = 0
    if getattr(args, "no_memory", False):
        out["memory"] = False
    if getattr(args, "council", False):
        out["council"] = True
    if getattr(args, "council_public", False):
        out["allow_public_council"] = True
    if getattr(args, "council_members", ""):
        out["council_members"] = tuple(
            part.strip() for part in args.council_members.split(",") if part.strip()
        )
    if getattr(args, "no_council_auto", False):
        out["council_auto"] = False
    if getattr(args, "library_dir", ""):
        out["library_dir"] = args.library_dir
    if getattr(args, "fleet", False):
        out["fleet"] = True
    if getattr(args, "no_scale", False):
        out["scale"] = False
    if getattr(args, "no_review", False):
        out["review_lessons"] = False
    if getattr(args, "no_security_review", False):
        out["security_review"] = False
    return out


# --------------------------------------------------------------------------
# setup helpers
# --------------------------------------------------------------------------
def prepare(args: argparse.Namespace) -> tuple:
    workspace_path = Path(getattr(args, "workspace", ".") or ".").expanduser().resolve()
    if not workspace_path.is_dir():
        raise SystemExit(f"workspace does not exist: {workspace_path}")
    config = load_config(workspace_path, overrides_from(args))
    config.workspace = str(workspace_path)
    ui = UI(config)
    workspace = Workspace(workspace_path)
    return config, ui, workspace


def connect(config: Config, ui: UI) -> Optional[LocalProvider]:
    try:
        provider = open_provider(config)
    except ProviderError as exc:
        ui.error(str(exc))
        return None
    ok, message = provider.health()
    if not ok:
        ui.error(message)
        ui.info("start one of these, then try again:")
        ui.info("  llama.cpp:  llama-server -m models/model.gguf --port 8080 --jinja")
        ui.info("  ollama:     ollama serve   (then: ollama pull qwen2.5-coder:7b)")
        ui.info("  LM Studio:  start the local server on port 1234")
        ui.info("or run `offgrid doctor` for a full check")
        return None
    ui.info(f"connected: {config.resolved_endpoint()} · model {provider.model}")

    if config.fleet:
        from .fleet import Fleet, FleetProvider

        fleet = Fleet(config).load()
        if not fleet.peers:
            ui.info("fleet is on, but no peers are registered yet")
            ui.info("  on the strong machine:  offgrid fleet serve")
            ui.info("  on this one:            offgrid fleet scan")
        else:
            answering = fleet.available(limit=3)
            if answering:
                ui.info(
                    "fleet: "
                    + ", ".join(f"{peer.name} ({peer.tier or '?'} tier)" for peer in answering)
                )
            else:
                ui.warn("fleet: no peer answered; everything runs on this machine")
        provider = FleetProvider(provider, fleet, config)
    return provider


def build_services(config: Config, workspace: Workspace) -> Dict[str, Any]:
    """Memory, offline library and council, shared by every agent in the run."""
    from .council import Council
    from .library import Library
    from .memory import MemoryStore
    from .semantics import load_lexicon

    lexicon = load_lexicon(Path(workspace.root) / config.lexicon_file)
    memory = None
    if config.memory:
        memory = MemoryStore(
            Path(workspace.root) / config.memory_dir / "memory.db",
            lexicon=lexicon,
        )
    library = Library(Path(workspace.root) / config.library_dir, lexicon=lexicon)
    return {"memory": memory, "library": library, "council": Council(config)}


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def _print_lesson(item: Any) -> None:
    """One pending lesson, with the verification evidence behind it if any."""
    print("  " + item.line())
    for line in item.evidence_lines():
        print(line)


def _review_pending(ui: UI, memory: Any, pending: list) -> tuple:
    """One decision per lesson. Nothing is stored as knowledge until accepted."""
    accepted = edited = rejected = 0
    for item in pending:
        print()
        _print_lesson(item)
        choice = ui.ask("  [a]ccept / [e]dit / [r]eject / [s]kip (enter = accept): ").strip().lower()
        if choice in ("", "a", "accept", "y", "yes"):
            memory.accept([item.id])
            accepted += 1
        elif choice in ("e", "edit"):
            replacement = ui.ask("  new wording: ").strip()
            if replacement and memory.edit(item.id, replacement):
                edited += 1
            else:
                ui.warn(f"#{item.id} kept pending as it was")
        elif choice in ("r", "reject", "n", "no"):
            if memory.reject([item.id]):
                rejected += 1
        else:
            ui.info(f"#{item.id} kept pending")
    return accepted, edited, rejected


def review_new_lessons(config: Config, ui: UI, team: Team) -> None:
    """Show what this run learned, before it is allowed to influence the next one."""
    memory = getattr(team, "memory", None)
    if memory is None or not config.memory:
        return
    pending = memory.pending(limit=12)
    if not pending:
        return
    ui.rule()
    ui.info(f"{len(pending)} new lesson(s) from this run. None of it is used until you accept it.")
    if not config.review_lessons or not sys.stdin.isatty():
        for item in pending:
            _print_lesson(item)
        ui.info("review when you like: offgrid memory review")
        return
    accepted, edited, rejected = _review_pending(ui, memory, pending)
    ui.ok(f"memory: {accepted} accepted, {edited} edited, {rejected} rejected")
    remaining = len(memory.pending(limit=100))
    if remaining:
        ui.info(f"{remaining} still pending: offgrid memory review")


def cmd_run(args: argparse.Namespace) -> int:
    config, ui, workspace = prepare(args)
    provider = connect(config, ui)
    if provider is None:
        return 2

    task = " ".join(args.task).strip()
    if not task:
        ui.error("empty task")
        return 2

    as_json = bool(getattr(args, "json", False))
    session = SessionLog(workspace.root, config.session_dir)
    team = Team(provider, workspace, config, ui, **build_services(config, workspace))
    if not as_json:
        ui.banner(task, provider.model, config.resolved_endpoint())
        ui.info(f"effort: {team.effort.name} - {team.effort.description}")
        if team.capability is not None and team.capacity is not None:
            ui.info(
                f"this machine: {team.capability.tier}"
                + (f", {team.capability.vram_gb:g} GB VRAM" if team.capability.vram_gb else "")
                + f" - {team.capacity.note}"
            )
    team.session_source = session.session_id
    try:
        result = team.run(task, verify_override=config.verify_command)
    except ProviderError as exc:
        ui.error(str(exc))
        return 2
    except KeyboardInterrupt:
        ui.warn("interrupted")
        return 130

    session.record_run(result)
    if as_json:
        payload = result.as_dict()
        payload["session"] = str(session.path)
        if hasattr(provider, "summary"):
            payload["compute"] = provider.summary()
        print(json.dumps(payload, indent=2))
        return 0 if result.verify_ok is not False else 1

    ui.rule()
    print()
    for line in (result.summary or "").splitlines():
        print("  " + line)
    print()
    if result.changed:
        ui.ok(f"{len(result.changed)} file(s) changed: {', '.join(result.changed[:8])}")
    if result.verify_ok is True:
        ui.ok(f"verified with `{result.verify_command}`")
    elif result.verify_ok is False:
        ui.error(f"verification failed: `{result.verify_command}`")
    else:
        ui.info("not verified: no test command found (pass --verify \"...\")")
    review_new_lessons(config, ui, team)
    for entry in result.council:
        first = str(entry.get("answer") or "").strip().splitlines()
        detail = next((line for line in first if line and not line.startswith("COUNCIL")), "")
        ui.info(f"council for {entry.get('agent')} ({', '.join(entry.get('members') or []) or 'no member'}): {detail[:140]}")
    for line in result.disclosures:
        ui.info(line)
    if hasattr(provider, "summary"):
        compute = provider.summary()
        if compute:
            ui.stats(compute)
    ui.stats(result.stats.summary() if result.stats else "")
    ui.stats(f"session log: {session.path}")
    return 0 if result.verify_ok is not False else 1


def cmd_chat(args: argparse.Namespace) -> int:
    config, ui, workspace = prepare(args)
    provider = connect(config, ui)
    if provider is None:
        return 2
    session = SessionLog(workspace.root, config.session_dir)
    team = Team(provider, workspace, config, ui, **build_services(config, workspace))
    team.session_source = session.session_id
    ui.info(
        f"effort: {team.effort.name}"
        + (
            f" | machine: {team.capacity.name} ({team.capability.tier})"
            if team.capacity is not None and team.capability is not None
            else ""
        )
    )
    if config.memory:
        remembered = team.memory.stats()["total"] if team.memory else 0
        if remembered:
            ui.info(f"carrying {remembered} remembered note(s) from earlier sessions")

    ui.rule()
    ui.info("describe what you want. /help for commands, /quit to leave.")
    ui.rule()
    while True:
        try:
            line = ui.ask(ui.paint("you> ", "\033[1m", "\033[36m"))
        except (EOFError, KeyboardInterrupt):
            print()
            break
        task = line.strip()
        if not task:
            continue
        if task.startswith("/"):
            command, _, rest = task[1:].partition(" ")
            if command in ("quit", "exit", "q"):
                break
            if command == "help":
                ui.info("/diff           show what changed in this session")
                ui.info("/files          list files changed")
                ui.info("/verify [cmd]   run the verification command")
                ui.info("/agents         show the team")
                ui.info("/memory [q]     what the agent remembers")
                ui.info("/library        offline library contents")
                ui.info("/status         model, endpoint, settings")
                ui.info("/quit           leave")
                continue
            if command == "diff":
                diff = workspace.diff()
                print(diff or "(no changes yet)")
                continue
            if command == "files":
                print("\n".join(workspace.changed_files()) or "(no changes yet)")
                continue
            if command == "memory":
                if team.memory is None:
                    ui.info("memory is off")
                else:
                    items = team.memory.recall(rest.strip(), limit=15) if rest.strip() else team.memory.all(limit=15)
                    print("\n".join(item.line() for item in items) or "(nothing remembered yet)")
                continue
            if command == "library":
                stats = team.library.stats() if team.library else {"files": 0, "categories": {}}
                ui.info(f"{stats['files']} document(s): {stats['categories'] or 'none'}")
                ui.info("fill it with: python3 scripts/fetch_library.py --get survival coding")
                continue
            if command == "verify":
                if rest.strip():
                    config.verify_command = rest.strip()
                command_text = config.verify_command or team.detect_verify_command()
                ok, output = team.verify(command_text)
                ui.info(output[:2000] if output else "nothing to verify")
                continue
            if command == "agents":
                print(describe_roster())
                continue
            if command == "status":
                print(config.describe())
                continue
            ui.warn(f"unknown command /{command}")
            continue

        try:
            result = team.run(task, verify_override=config.verify_command)
        except ProviderError as exc:
            ui.error(str(exc))
            continue
        except KeyboardInterrupt:
            ui.warn("interrupted; workspace changes are kept")
            continue
        session.record_run(result)
        ui.rule()
        for line_text in (result.summary or "").splitlines():
            print("  " + line_text)
        if result.changed:
            ui.info("changed: " + ", ".join(result.changed[:8]))
        for line in result.disclosures:
            ui.info(line)
        review_new_lessons(config, ui, team)
        ui.rule()

    ui.info(f"session log: {session.path}")
    return 0


def _print_readiness(report: Any, ui: UI, heading: str = "") -> None:
    """Print the continuity checks used by both doctor and protect --check."""
    if heading:
        print()
        print(f"  {heading}")
    else:
        print()
    for check in report.checks:
        mark = "ok  " if check.ok else "FAIL"
        print(f"  {mark} {check.name}: {check.detail}")
        if not check.ok and check.fix:
            print(f"       fix: {check.fix}")
    print()
    if report.ready:
        ui.ok("ready for a continuity handoff")
    else:
        ui.warn("not ready; fix the failed check(s) above")


def cmd_doctor(args: argparse.Namespace) -> int:
    config, ui, workspace = prepare(args)
    print()
    print(ui.paint(f"  OffGrid {__version__} doctor", "\033[1m"))
    print()
    print(f"  python      {sys.version.split()[0]}")
    print(f"  platform    {sys.platform}")
    print(f"  workspace   {workspace.root}")
    print(f"  git         {'yes' if (workspace.root / '.git').exists() else 'no'}")
    print(f"  config      {config.describe()}")
    capability = probe(config.workspace)
    print(f"  hardware    {capability.tier}, {capability.cpu_count} cores, {capability.ram_gb:g} GB RAM, "
          + (", ".join(capability.accelerators) or "no GPU"))
    print(f"  runs        {recommend(capability)}")
    effort = resolve_effort(config)
    if config.scale:
        capacity = capacity_for(capability)
        effort = adjust_effort(effort, capacity)
        print(f"  capacity    {describe_capacity(capacity, effort)}")
    print(f"  effort      {effort.name}: {effort.description}")
    from .fleet import Fleet

    fleet = Fleet(config).load()
    if not fleet.peers:
        print("  fleet       off, no peers registered")
    else:
        live = fleet.available(limit=9, timeout=1)
        print(f"  fleet       {len(live)}/{len(fleet.peers)} peer(s) answering: "
              + (", ".join(f"{peer.name} ({peer.tier or '?'})" for peer in live) or "none right now"))
    print()

    print("  scanning localhost for model servers...")
    found = discover(config)
    if not found:
        ui.warn("no local model server found")
        ui.info("install one (all free):")
        ui.info("  Ollama            https://ollama.com/download  ->  ollama pull qwen2.5-coder:7b")
        ui.info("  llama.cpp         build it, then: llama-server -m model.gguf --port 8080 --jinja")
        ui.info("  LM Studio         https://lmstudio.ai")
        ui.info("or run: python3 scripts/setup_usb.py --dry-run")
    else:
        for endpoint, names in found:
            ui.ok(f"{endpoint}  models: {', '.join(names[:6]) or '(none)'}")

    if config.endpoint and not any(endpoint == config.endpoint for endpoint, _ in found):
        ui.warn(f"configured endpoint {config.endpoint} is not answering")

    print()
    print("  checks")
    if (workspace.root / ".offgrid.json").is_file():
        ui.ok("workspace config found")
    else:
        ui.info("no .offgrid.json in the workspace (optional)")
    if config.command_policy == "deny":
        ui.info("shell access disabled by configuration")
    from . import primary as primary_module

    signing_key_path = (
        Path(args.sign_key).expanduser() if getattr(args, "sign_key", "") else None
    )
    readiness = primary_module.readiness(
        config, workspace.root, signing_key_path=signing_key_path
    )
    _print_readiness(readiness, ui, heading="continuity")
    print()
    return 0 if found or config.endpoint else 1


def cmd_models(args: argparse.Namespace) -> int:
    config, ui, _workspace = prepare(args)
    provider = connect(config, ui)
    if provider is None:
        return 2
    try:
        names = provider.models()
    except ProviderError as exc:
        ui.error(str(exc))
        return 2
    if not names:
        ui.warn("server reported no models")
        return 1
    for name in names:
        marker = "*" if name == provider.model else " "
        print(f" {marker} {name}")
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    print()
    print("  the team (each one is a separate prompt, tool set and turn budget)")
    print(describe_roster())
    print()
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    config, _ui, _workspace = prepare(args)
    print(json.dumps(config.as_dict(), indent=2))
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    config, ui, workspace = prepare(args)
    from .memory import MemoryStore
    from .semantics import load_lexicon

    lexicon = load_lexicon(Path(workspace.root) / config.lexicon_file)
    for warning in lexicon.warnings:
        ui.warn(warning)
    store = MemoryStore(
        Path(workspace.root) / config.memory_dir / "memory.db",
        lexicon=lexicon,
    )
    action = (args.action or "list").lower()
    terms = " ".join(getattr(args, "terms", []) or []).strip()

    if action == "stats":
        print(json.dumps(store.stats(), indent=2))
        return 0
    if action == "rejected":
        items = store.rejected(limit=100, query=terms)
        if not items:
            ui.info("no rejected lessons in the ledger")
            return 1 if terms else 0
        for item in items:
            print("  " + item.line())
        ui.info(
            "these never enter prompts; the archivist is told not to propose them again"
        )
        return 0
    if action == "review":
        pending = store.pending(limit=40)
        if not pending:
            ui.info("nothing pending; new lessons appear after a run")
            return 0
        if not sys.stdin.isatty():
            for item in pending:
                _print_lesson(item)
            ui.info("then: offgrid memory accept ID / reject ID / forgive ID|TEXT / edit ID TEXT")
            return 0
        accepted, edited, rejected = _review_pending(ui, store, pending)
        ui.ok(f"memory: {accepted} accepted, {edited} edited, {rejected} rejected")
        return 0
    if action in ("accept", "reject", "forgive"):
        if not terms:
            usage = (
                "offgrid memory forgive ID [ID...] | TEXT [QUERY]"
                if action == "forgive"
                else f"offgrid memory {action} ID [ID...]"
            )
            ui.error(f"which lesson? usage: {usage}")
            return 2
        id_terms = terms.split()
        if action == "forgive" and not all(term.isdigit() for term in id_terms):
            changed = store.forgive_matching(terms)
            if changed:
                print(
                    f"forgave {changed} rejection record(s) matching {terms!r}; "
                    "nothing was restored to active memory"
                )
                return 0
            ui.error(f"no rejection ledger entry matches: {terms}")
            return 1
        ids = [int(term) for term in id_terms if term.isdigit()]
        if not ids:
            ui.error(f"which lesson? usage: offgrid memory {action} ID [ID...]")
            return 2
        if action == "accept":
            changed = store.accept(ids)
        elif action == "reject":
            changed = store.reject(ids)
        else:
            changed = store.forgive(ids)
        if action == "accept":
            print(f"accepted {changed} lesson(s)")
        elif action == "reject":
            print(f"rejected and recorded in the ledger {changed} lesson(s)")
        else:
            print(
                f"forgave {changed} rejection record(s); "
                "nothing was restored to active memory"
            )
        return 0 if changed else 1
    if action == "edit":
        parts = terms.split(None, 1)
        if len(parts) < 2 or not parts[0].isdigit():
            ui.error("usage: offgrid memory edit ID NEW WORDING")
            return 2
        item = store.edit(int(parts[0]), parts[1])
        if item is None:
            ui.error("no lesson with that id")
            return 1
        print("edited and accepted: " + item.text)
        return 0
    if action == "add":
        if not terms:
            ui.error("nothing to remember; say what to store")
            return 2
        new_id = store.remember(terms, source="user", confidence=0.9)
        print(f"remembered as #{new_id}" if new_id else "already remembered")
        return 0
    if action == "forget":
        ids = [int(term) for term in terms.split() if term.isdigit()]
        removed = sum(1 for ident in ids if store.forget(ident))
        print(f"forgot {removed} entr{'y' if removed == 1 else 'ies'}")
        return 0 if removed else 1
    if action == "search":
        items = store.recall(terms, limit=20)
        if not items:
            print("nothing matches that")
            return 1
        for item in items:
            reason = f"  [{item.reason}]" if item.reason else ""
            print("  " + item.line() + reason)
        return 0

    items = store.all(limit=40)
    pending = store.pending(limit=100)
    if not items:
        ui.info("memory is empty; it fills up as you use OffGrid")
    for item in items:
        print("  " + item.line())
    if pending:
        ui.info(f"{len(pending)} lesson(s) pending review: offgrid memory review")
    stats = store.stats()
    if stats.get("rejected"):
        ui.info(
            f"{stats['rejected']} rejected lesson(s) kept in the ledger: "
            "offgrid memory rejected"
        )
    return 0


def cmd_lexicon(args: argparse.Namespace) -> int:
    config, ui, workspace = prepare(args)
    from .semantics import load_lexicon

    path = Path(workspace.root) / config.lexicon_file
    lexicon = load_lexicon(path)
    groups = {name: list(words) for name, words in sorted(lexicon.groups.items())}
    payload = {
        "path": str(path),
        "source": lexicon.source,
        "found": path.is_file(),
        "warnings": list(lexicon.warnings),
        "groups": groups,
    }
    strict_failed = bool(getattr(args, "strict", False)) and bool(lexicon.warnings)
    if args.json:
        print(json.dumps(payload, indent=2))
        return 1 if strict_failed else 0

    print()
    print(ui.paint("  merged meaning lexicon", "\033[1m"))
    if lexicon.source:
        print(f"  source file: {lexicon.source}")
    else:
        print(f"  source file: {path} (not found; built-in groups only)")
    print(f"  groups: {len(groups)}  words: {sum(len(words) for words in groups.values())}")
    print()
    for name, words in groups.items():
        print(f"  {name}: {', '.join(words)}")
    print()
    if lexicon.warnings:
        for warning in lexicon.warnings:
            ui.warn(warning)
    else:
        print("  warnings: none")
    print()
    return 1 if strict_failed else 0


def cmd_library(args: argparse.Namespace) -> int:
    config, ui, workspace = prepare(args)
    from .library import COLLECTIONS, Library
    from .semantics import load_lexicon

    lexicon = load_lexicon(Path(workspace.root) / config.lexicon_file)
    for warning in lexicon.warnings:
        ui.warn(warning)
    library = Library(Path(workspace.root) / config.library_dir, lexicon=lexicon)
    action = (args.action or "list").lower()
    terms = " ".join(getattr(args, "terms", []) or []).strip()

    if action == "search":
        hits = library.search(terms)
        print("\n".join(hits) or "no matches (is anything downloaded?)")
        return 0 if hits else 1
    if action == "index":
        data = library.index()
        print(f"indexed {len(data['entries'])} file(s) -> {library.index_path()}")
        return 0
    if action == "verify":
        failures = 0
        for entry in COLLECTIONS:
            ok, message = library.verify(entry)
            print(f"  {'ok  ' if ok else 'FAIL'} {entry.id:<14} {message}")
            failures += 0 if ok else 1
        return 0 if failures == 0 else 1

    stats = library.stats()
    have = {item.get("collection") for item in library.downloaded()}
    print(f"  library: {stats['root']}")
    print(f"  {stats['files']} file(s) indexed, {stats['bytes'] / 1e6:.1f} MB")
    print()
    for entry in COLLECTIONS:
        mark = "*" if entry.id in have else " "
        print(f" {mark} {entry.id:<14} {entry.category:<10} ~{entry.approx_gb:>4.1f} GB  {entry.title}")
    print()
    print("  download: python3 scripts/fetch_library.py --get survival coding")
    return 0


def cmd_council(args: argparse.Namespace) -> int:
    config, ui, _workspace = prepare(args)
    from .council import Council, CouncilUnavailable

    council = Council(config)
    action = (args.action or "members").lower()
    terms = " ".join(getattr(args, "terms", []) or []).strip()

    if action == "ask":
        if not terms:
            ui.error("ask what?")
            return 2
        if not config.council:
            ui.warn("the council is off; re-run with --council (add --council-public for public members)")
            return 2
        try:
            answer = council.ask(terms)
        except CouncilUnavailable as exc:
            ui.error(str(exc))
            return 1
        print(answer.render())
        return 0 if answer.usable else 1
    if action == "ping":
        results = council.ping()
        if not results:
            ui.info("no council members configured")
        for name, ok, note in results:
            print(f"  {'ok  ' if ok else 'FAIL'} {name:<28} {note}")
        return 0

    members, skipped = council.selected()
    if not members:
        ui.info("no members available; the default member is 'local', your running model")
    for member in members:
        scope = "public: prompts leave this machine" if member.public else "local"
        model = f" [model {member.model}]" if member.model else ""
        print(f"  {member.name:<28} {member.endpoint}{model}  ({scope})")
    for item in skipped:
        ui.info("skipped: " + item)
    ui.info("enable with --council, and --council-public for keyless public members")
    return 0


def cmd_hardware(args: argparse.Namespace) -> int:
    config, ui, workspace = prepare(args)
    capability = probe(config.workspace, refresh=bool(getattr(args, "refresh", False)))
    capacity = capacity_for(capability)
    profile = resolve_effort(config)
    effort = adjust_effort(profile, capacity) if config.scale else profile
    suggestion = recommend(capability)

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "capability": capability.as_dict(),
                    "recommendation": suggestion,
                    "capacity": capacity.as_dict() if config.scale else None,
                    "effort": effort.as_dict(),
                },
                indent=2,
            )
        )
        return 0

    print()
    for line in capability.describe():
        print("  " + line)
    print()
    print(f"  model       {suggestion}")
    if config.scale:
        print(f"  capacity    {describe_capacity(capacity, effort)}")
    else:
        print("  capacity    scaling is off (--no-scale): the effort profile is used as written")
    print(f"  effort      {effort.name} - {effort.description}")
    print()
    return 0


def cmd_fleet(args: argparse.Namespace) -> int:
    config, ui, workspace = prepare(args)
    from .fleet import (
        Fleet,
        FleetError,
        FleetServer,
        lan_address,
        load_or_create_token,
        peer_from_discovery,
    )

    fleet = Fleet(config).load()
    action = (args.action or "list").lower()
    terms = list(getattr(args, "terms", []) or [])

    if action == "add":
        if len(terms) < 2:
            ui.error("usage: offgrid fleet add NAME URL [TOKEN]")
            return 2
        token = terms[2] if len(terms) > 2 else config.fleet_token
        try:
            peer = fleet.add(terms[0], terms[1], token=token)
        except FleetError as exc:
            ui.error(str(exc))
            return 2
        if fleet.ping(peer):
            ui.ok(f"{peer.name} answers: {peer.tier or 'unknown'} tier, {peer.ram_gb:g} GB RAM")
        else:
            ui.warn(f"{peer.name} saved but not answering yet: {peer.last_error}")
        return 0

    if action == "remove":
        if not terms:
            ui.error("usage: offgrid fleet remove NAME")
            return 2
        return 0 if fleet.remove(terms[0]) else 1

    if action == "ping":
        if not fleet.peers:
            ui.info("no peers configured yet")
            return 1
        for peer in fleet.ping_all():
            state = "ok  " if peer.healthy else "FAIL"
            detail = f"{peer.latency_ms:.0f} ms" if peer.healthy else peer.last_error
            print(f"  {state} {peer.name:<12} {peer.url:<28} {detail}")
        return 0

    if action == "scan":
        as_json = bool(getattr(args, "json", False))
        found = fleet.discover()
        if not found:
            if as_json:
                print(
                    json.dumps(
                        {
                            "action": "scan",
                            "counts": {
                                "discovered": 0,
                                "registered": 0,
                                "known": 0,
                                "skipped": 0,
                            },
                            "peers": [],
                        },
                        indent=2,
                    )
                )
            else:
                ui.info("no OffGrid peers answered on this network")
                ui.info("on the other machine, start one with: offgrid fleet serve")
            return 1

        token = str(args.token or config.fleet_token or "")
        auto = bool(getattr(args, "yes", False))
        if as_json:
            interactive = False
        else:
            try:
                interactive = sys.stdin.isatty()
            except Exception:
                interactive = False
        registered = skipped = known = 0
        seen = set()
        results: List[Dict[str, Any]] = []

        def scan_result(
            peer: Any, outcome: str, reason: str = "", **extra: Any
        ) -> Dict[str, Any]:
            """One discovered peer and what happened to it, without its token."""
            data: Dict[str, Any] = {
                "name": peer.name if peer is not None else str(extra.get("name") or "peer"),
                "url": peer.url if peer is not None else "",
                "tier": peer.tier if peer is not None else "",
                "ram_gb": peer.ram_gb if peer is not None else 0.0,
                "vram_gb": peer.vram_gb if peer is not None else 0.0,
                "accelerators": list(peer.accelerators) if peer is not None else [],
                "models": list(peer.models) if peer is not None else [],
                "outcome": outcome,
                "reason": reason,
                "reachable": bool(peer.healthy) if peer is not None else False,
                "latency_ms": peer.latency_ms if peer is not None else 0.0,
                "last_error": peer.last_error if peer is not None else "",
            }
            data.update(extra)
            return data

        if not as_json:
            print()
        for entry in found:
            try:
                candidate = peer_from_discovery(
                    entry, token=token, fallback_port=config.fleet_port
                )
            except (FleetError, TypeError, ValueError) as exc:
                if not as_json:
                    ui.warn(f"skipped a discovery reply: {exc}")
                results.append(
                    scan_result(
                        None,
                        "error",
                        str(exc),
                        name=str(entry.get("name") or "peer")[:80],
                    )
                )
                skipped += 1
                continue
            url = candidate.url
            if url in seen:
                continue
            seen.add(url)

            existing = next((peer for peer in fleet.peers if peer.url == url), None)
            if existing is not None:
                results.append(
                    scan_result(existing, "already_registered", "already registered")
                )
                if not as_json:
                    print(f"  {existing.name}  {url}  already registered")
                known += 1
                continue

            name = fleet.unique_name(candidate.name)
            detail = f"{entry.get('tier', '?')} tier"
            if entry.get("ram_gb"):
                detail += f", {entry.get('ram_gb')} GB RAM"
            if entry.get("vram_gb"):
                detail += f", {entry.get('vram_gb')} GB VRAM"
            if not as_json:
                print(f"  found: {name}  {url}  {detail}")

            if not auto:
                if as_json:
                    results.append(
                        scan_result(
                            candidate,
                            "skipped",
                            "confirmation required (pass --yes)",
                            name=name,
                        )
                    )
                    skipped += 1
                    continue
                if not interactive:
                    print(
                        "    not registered: no terminal for confirmation "
                        "(use --yes or `fleet add`)"
                    )
                    skipped += 1
                    continue
                answer = ui.ask(f"  register {name}? [Y/n]: ").strip().lower()
                if answer not in ("", "y", "yes"):
                    skipped += 1
                    continue

            if not token:
                if as_json:
                    results.append(
                        scan_result(
                            candidate,
                            "skipped",
                            "shared token required (pass --token)",
                            name=name,
                        )
                    )
                    skipped += 1
                    continue
                if not interactive:
                    print("    not registered: a shared token is required (use --token ...)")
                    skipped += 1
                    continue
                token = getpass.getpass(f"  shared token for {name} (hidden): ").strip()
                if not token:
                    ui.warn(f"{name} not registered: a shared token is required")
                    skipped += 1
                    continue

            candidate.name = name
            candidate.token = token
            try:
                fleet.register(candidate)
            except FleetError as exc:
                if not as_json:
                    ui.warn(f"{name} not registered: {exc}")
                results.append(scan_result(candidate, "error", str(exc)))
                skipped += 1
                continue
            registered += 1
            reachable = fleet.ping(candidate)
            if not as_json:
                if reachable:
                    ui.ok(
                        f"registered {candidate.name} ({candidate.tier or 'unknown'} tier, "
                        f"{candidate.ram_gb:g} GB RAM, {candidate.latency_ms:.0f} ms)"
                    )
                else:
                    ui.warn(
                        f"registered {candidate.name}; health check failed: "
                        f"{candidate.last_error}"
                    )
            results.append(
                scan_result(
                    candidate,
                    "registered",
                    "" if reachable else (candidate.last_error or "health check failed"),
                    reachable=reachable,
                )
            )
            fleet.save()

        if as_json:
            print(
                json.dumps(
                    {
                        "action": "scan",
                        "counts": {
                            "discovered": len(found),
                            "registered": registered,
                            "known": known,
                            "skipped": skipped,
                        },
                        "peers": results,
                    },
                    indent=2,
                )
            )
            return 0 if registered or known else 1

        print()
        if registered:
            ui.ok(f"scan: {registered} registered, {known} already known, {skipped} skipped")
            ui.info("use `offgrid run ... --fleet` to route work to them")
        elif known:
            ui.info(f"scan: {known} already known, {skipped} skipped")
        else:
            ui.info(f"scan: nothing registered, {skipped} skipped")
        return 0 if registered or known else 1

    if action == "serve":
        provider = connect(config, ui)
        if provider is None:
            return 2
        token = args.token or load_or_create_token(config)
        server = FleetServer(config, provider, token=token, port=args.port or config.fleet_port)
        port = server.start()
        address = lan_address() or "this-host"
        print()
        ui.ok(f"serving this machine's model on port {port} (LAN only, token required)")
        ui.info(f"  info:   http://{address}:{port}/fleet/info")
        ui.info(f"  add:    offgrid fleet add <name> http://{address}:{port} --token {token}")
        ui.info("  ctrl-c to stop")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            ui.info("stopping")
        finally:
            server.stop()
        return 0

    print()
    for line in fleet.describe():
        print("  " + line)
    print()
    if not fleet.peers:
        ui.info("on the strong machine:  offgrid fleet serve")
        ui.info("then discover it here:  offgrid fleet scan")
    return 0


def _security_review_watch(config: Config, ui: UI, workspace: Workspace, args: argparse.Namespace) -> int:
    """Re-review the workspace on an interval, showing only newly appearing findings."""
    from . import audit

    interval = getattr(args, "interval", None)
    try:
        interval = 300.0 if interval is None else float(interval)
    except (TypeError, ValueError):
        ui.error("--interval must be a number of seconds")
        return 2
    if interval <= 0:
        ui.error("--interval must be greater than zero")
        return 2

    once = bool(getattr(args, "once", False))
    history_path = workspace.root / config.security_history
    try:
        while True:
            entries = audit.history(history_path, limit=1)
            previous = entries[-1] if entries else None
            current = audit.review_workspace(workspace.root, config=config)
            fresh = audit.new_findings((previous or {}).get("findings") or [], current)
            # Each pass becomes the baseline for the next one.
            audit.record(history_path, current, source="watch")

            payload = {
                "timestamp": round(time.time(), 3),
                "baseline": previous.get("t") if previous else None,
                "baseline_source": previous.get("source") if previous else None,
                "source": "watch",
                "findings": [finding.as_dict() for finding in fresh],
                "counts": audit.counts(fresh),
                "reviewed": len(current),
                "recorded": str(history_path),
            }
            if args.json:
                print(json.dumps(payload, indent=2 if once else None), flush=True)
            else:
                ui.rule()
                if previous is None:
                    ui.info("first workspace security review; no earlier review to compare against")
                else:
                    stamp = time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(float(previous.get("t") or 0))
                    )
                    ui.info(
                        f"workspace re-reviewed; findings new since {stamp} "
                        f"({previous.get('source', '?')})"
                    )
                if fresh:
                    for finding in fresh:
                        print("  " + finding.line_text())
                        if finding.remediation:
                            print(f"      fix: {finding.remediation}")
                else:
                    ui.ok("no new findings")
                counts = audit.counts(fresh)
                ui.info(
                    f"{counts['total']} new finding(s); {len(current)} total in this review "
                    f"- logged to {history_path}"
                )

            if once:
                return 2 if audit.verdict(fresh) == "critical" else 0
            time.sleep(interval)
    except KeyboardInterrupt:
        ui.info("security review watch stopped")
        return 130


def _security_timer_install(ui: UI, workspace: Workspace, args: argparse.Namespace) -> int:
    from .timer import TimerError, install_timer

    interval = 300 if getattr(args, "interval", None) is None else args.interval
    try:
        info = install_timer(workspace.root, interval=interval)
    except (TimerError, ValueError) as exc:
        ui.error(str(exc))
        return 1
    if args.json:
        print(json.dumps(info.as_dict(), indent=2))
    else:
        ui.ok(f"systemd user timer installed: {info.timer_name}")
        ui.info(f"  runs every {info.interval}s in {info.workspace}")
        ui.info(f"  service: {info.service_name}")
        ui.info(f"  logs: journalctl --user -u {info.service_name}")
        ui.info("  remove: offgrid security watch --remove-timer")
    return 0


def _security_timer_remove(ui: UI, workspace: Workspace, args: argparse.Namespace) -> int:
    from .timer import TimerError, remove_timer

    try:
        result = remove_timer(workspace.root)
    except TimerError as exc:
        ui.error(str(exc))
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        ui.ok(f"removed systemd user timer: {result['timer_name']}")
        if not result["systemctl"]:
            ui.info("systemctl was unavailable; run `systemctl --user daemon-reload` if needed")
    return 0


def _security_service_interval(args: argparse.Namespace) -> float:
    return 300 if getattr(args, "interval", None) is None else args.interval


def _security_service_file(ui: UI, workspace: Workspace, args: argparse.Namespace) -> int:
    from .timer import TimerError, write_service

    try:
        info = write_service(
            workspace.root,
            interval=_security_service_interval(args),
            service_file=getattr(args, "service_file", None) or None,
        )
    except (TimerError, ValueError) as exc:
        ui.error(str(exc))
        return 1
    if args.json:
        print(json.dumps(info.as_dict(), indent=2))
    else:
        ui.ok(f"continuous security service file written: {info.service_path}")
        ui.info(f"  review loop every {info.interval}s in {info.workspace}")
        ui.info(f"  install it with: systemctl --user enable --now {info.service_name}")
    return 0


def _security_service_install(ui: UI, workspace: Workspace, args: argparse.Namespace) -> int:
    from .timer import TimerError, install_service

    try:
        info = install_service(
            workspace.root,
            interval=_security_service_interval(args),
        )
    except (TimerError, ValueError) as exc:
        ui.error(str(exc))
        return 1
    if args.json:
        print(json.dumps(info.as_dict(), indent=2))
    else:
        ui.ok(f"systemd user service installed: {info.service_name}")
        ui.info(f"  continuously reviews {info.workspace} every {info.interval}s")
        ui.info(f"  service: {info.service_path}")
        ui.info(f"  logs: journalctl --user -u {info.service_name}")
        ui.info("  remove: offgrid security watch --remove-service")
    return 0


def _security_service_remove(ui: UI, workspace: Workspace, args: argparse.Namespace) -> int:
    from .timer import TimerError, remove_service

    try:
        result = remove_service(workspace.root)
    except TimerError as exc:
        ui.error(str(exc))
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        ui.ok(f"removed systemd user service: {result['service_name']}")
        if not result["systemctl"]:
            ui.info("systemctl was unavailable; run `systemctl --user daemon-reload` if needed")
    return 0


def cmd_security(args: argparse.Namespace) -> int:
    config, ui, workspace = prepare(args)
    from . import audit, netmap, vault

    action = (args.action or "review").lower()
    if action == "plan":
        print(vault.plan())
        return 0

    if action == "watch":
        if getattr(args, "remove_service", False):
            return _security_service_remove(ui, workspace, args)
        if getattr(args, "remove_timer", False):
            return _security_timer_remove(ui, workspace, args)
        if getattr(args, "install_service", False):
            return _security_service_install(ui, workspace, args)
        if getattr(args, "service_file", None) is not None:
            return _security_service_file(ui, workspace, args)
        if getattr(args, "install_timer", False):
            return _security_timer_install(ui, workspace, args)
        if (
            getattr(args, "watch_reviews", False)
            or getattr(args, "interval", None) is not None
            or getattr(args, "once", False)
        ):
            return _security_review_watch(config, ui, workspace, args)

        from . import watch as watch_module

        path = workspace.root / config.watch_file
        current = watch_module.capture()
        previous = watch_module.load(path)
        if previous is None:
            watch_module.save(current, path)
            if args.json:
                print(
                    json.dumps(
                        {
                            "baseline": None,
                            "saved": str(path),
                            "current": current.as_dict(),
                            "note": watch_module.NOTE,
                        },
                        indent=2,
                    )
                )
            else:
                ui.info(f"baseline saved to {path} ({len(current.connections)} connection(s))")
                ui.info(watch_module.NOTE)
            return 0

        changes = watch_module.compare(previous, current)
        if getattr(args, "save", False):
            watch_module.save(current, path)
        if args.json:
            print(
                json.dumps(
                    {
                        "baseline": previous.as_dict(),
                        "current": current.as_dict(),
                        "changes": [change.as_dict() for change in changes],
                        "note": watch_module.NOTE,
                    },
                    indent=2,
                )
            )
            return watch_module.exit_code(changes)
        print()
        for line in watch_module.render(previous, current, changes):
            print(line)
        print()
        if getattr(args, "save", False):
            ui.info(f"baseline updated: {path}")
        high = [change for change in changes if change.severity == "high"]
        if high:
            ui.warn(f"{len(high)} new listener(s) reachable from your network")
        return watch_module.exit_code(changes)

    if action == "map":
        import socket

        connections, source = netmap.collect()
        host = socket.gethostname() or "this machine"
        lines = netmap.render_map(connections, host)
        if args.json:
            print(
                json.dumps(
                    {
                        "host": host,
                        "source": source,
                        "connections": [item.as_dict() for item in connections],
                        "map": lines,
                    },
                    indent=2,
                )
            )
            return 0
        print()
        for line in lines:
            print(line)
        print()
        for line in netmap.describe_sources(connections, source):
            print("  " + line)
        return 0

    if action == "history":
        entries = audit.history(workspace.root / config.security_history, limit=20)
        if args.json:
            print(json.dumps(entries, indent=2))
            return 0
        if not entries:
            ui.info("no reviews recorded yet; run `offgrid security review` or any agent run")
            return 0
        print()
        for entry in entries:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.get("t", 0)))
            summary = entry.get("counts") or {}
            print(
                f"  {stamp}  {entry.get('verdict', '?'):<8} "
                f"total={summary.get('total', 0)} critical={summary.get('critical', 0)}  "
                f"({entry.get('source', '?')})"
            )
        return 0

    if action in ("encrypt", "decrypt"):
        target = " ".join(getattr(args, "terms", []) or []).strip()
        if not target:
            ui.error(f"usage: offgrid security {action} PATH [--passphrase-env NAME]")
            return 2
        passphrase = os.environ.get(args.passphrase_env, "") if args.passphrase_env else ""
        if not passphrase:
            if sys.stdin.isatty():
                import getpass

                passphrase = getpass.getpass("passphrase: ")
                if action == "encrypt" and getpass.getpass("again: ") != passphrase:
                    ui.error("passphrases did not match")
                    return 2
            else:
                ui.error(
                    "no passphrase: set --passphrase-env NAME. A passphrase passed as an argument "
                    "would end up in your shell history."
                )
                return 2
        try:
            if action == "encrypt":
                output = vault.encrypt_path(Path(target), passphrase, workspace=workspace.root)
                ui.ok(f"encrypted to {output}")
                ui.info("the plaintext is still on disk: encryption does not erase it")
            else:
                destination = Path(args.out) if args.out else None
                output = vault.decrypt_path(Path(target), passphrase, out=destination)
                ui.ok(f"restored to {output}")
        except vault.VaultError as exc:
            ui.error(str(exc))
            return 1
        return 0

    changed = workspace.changed_files() if getattr(args, "changed", False) else None
    findings = audit.review_workspace(workspace.root, changed=changed, config=config)
    audit.record(workspace.root / config.security_history, findings, source="cli")
    if args.json:
        print(
            json.dumps(
                {
                    "verdict": audit.verdict(findings),
                    "counts": audit.counts(findings),
                    "findings": [item.as_dict() for item in findings],
                },
                indent=2,
            )
        )
        return 2 if audit.verdict(findings) == "critical" else 0
    print()
    if not findings:
        ui.ok("no findings")
    for finding in findings:
        print("  " + finding.line_text())
        if finding.remediation:
            print(f"      fix: {finding.remediation}")
    print()
    summary = audit.counts(findings)
    ui.info(
        f"{summary['total']} finding(s): {summary['critical']} critical, {summary['high']} high, "
        f"{summary['medium']} medium - logged to {workspace.root / config.security_history}"
    )
    return 2 if audit.verdict(findings) == "critical" else 0


def cmd_protect(args: argparse.Namespace) -> int:
    config, ui, workspace = prepare(args)
    from . import primary as primary_module

    sign_key_arg = getattr(args, "sign_key", "") or ""
    signing_key_path = Path(sign_key_arg).expanduser() if sign_key_arg else None

    if getattr(args, "check", False):
        max_age_days = getattr(args, "max_age_days", 30.0)
        if max_age_days <= 0:
            ui.error("--max-age-days must be greater than zero")
            return 2
        kit_path = (
            Path(args.out).expanduser()
            if args.out
            else workspace.root / config.session_dir / "continuity.md"
        )
        report = primary_module.readiness(
            config,
            workspace.root,
            kit_path=kit_path,
            backup_paths=getattr(args, "backup", []) or [],
            max_age_days=max_age_days,
            signing_key_path=signing_key_path,
        )
        if args.json:
            print(json.dumps(report.as_dict(), indent=2))
            return 0 if report.ready else 1
        _print_readiness(report, ui)
        return 0 if report.ready else 1

    backup_paths = getattr(args, "backup", []) or []
    if sign_key_arg and getattr(args, "kit", False):
        ui.error("--sign-key cannot be combined with --kit; use it with --backup or --check")
        return 2
    if sign_key_arg and not backup_paths and not getattr(args, "check", False):
        ui.error("--sign-key requires --backup or --check")
        return 2
    if backup_paths and getattr(args, "kit", False):
        ui.error("--backup cannot be combined with --kit; use it on its own or with --check")
        return 2
    if backup_paths and not getattr(args, "check", False):
        if len(backup_paths) != 1:
            ui.error("protect --backup takes exactly one destination directory")
            return 2
        kit_path = (
            Path(args.out).expanduser()
            if args.out
            else workspace.root / config.session_dir / "continuity.md"
        )
        try:
            report = primary_module.backup_files(
                config,
                workspace.root,
                Path(backup_paths[0]),
                kit_path=kit_path,
                force=bool(args.force),
                sign_key=signing_key_path,
            )
        except primary_module.BackupError as exc:
            ui.error(str(exc))
            return 1
        if args.json:
            print(json.dumps(report.as_dict(), indent=2))
        else:
            ui.ok(f"backed up the continuity kit and local model to {report.target}")
            ui.info(f"  kit: {report.kit_copy}")
            ui.info(f"  model: {report.model_copy}")
            ui.info(f"  manifest: {report.manifest} (SHA-256)")
            if report.signature:
                ui.info(f"  signature: {report.signature} (HMAC-SHA256)")
                if report.signing_key_created:
                    ui.warn(f"  signing key created: {report.signing_key}")
                    ui.info(
                        "  keep the signing key off the backup drive; without it the "
                        "signature cannot be verified."
                    )
                else:
                    ui.info(f"  signing key: {report.signing_key}")
                ui.info(
                    "  verify this copy with `offgrid protect --check --backup <same path> "
                    "--sign-key <key>`."
                )
            else:
                ui.info(
                    "  verify this copy with `offgrid protect --check --backup <same path>`."
                )
            ui.info(
                "  this path cannot prove it is off this machine; verify the drive "
                "is not the workspace disk and keep another copy elsewhere."
            )
        return 0

    if getattr(args, "kit", False):
        target = Path(args.out) if args.out else workspace.root / config.session_dir / "continuity.md"
        if target.exists() and not args.force:
            ui.error(f"{target} already exists; pass --force to replace it")
            return 1
        text = primary_module.continuity_kit(config, workspace.root)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            try:
                target.chmod(0o600)
            except OSError:
                pass
        except OSError as exc:
            ui.error(f"could not write {target}: {exc}")
            return 1
        ui.ok(f"household continuity kit written to {target}")
        ui.info("fill in the blanks by hand. It contains personal details; keep it private.")
        ui.info("never write a passphrase in it, and keep a copy off this machine.")
        return 0

    posture = primary_module.protection(config, workspace.root)
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "principal": config.principal,
                    "household": list(config.household),
                    "posture": posture,
                    "limits": [line for line in primary_module.LIMITS.splitlines() if line.strip()],
                },
                indent=2,
            )
        )
        return 0

    print()
    print(ui.paint("  whose side this software is on", "\033[1m"))
    for line in primary_module.PRIMARY.splitlines():
        print("  " + line)
    print()
    print(ui.paint("  protections in place", "\033[1m"))
    for line in posture:
        print("  " + line)
    print()
    print(ui.paint("  limits", "\033[1m"))
    for line in primary_module.LIMITS.splitlines():
        print("  " + line)
    print()
    return 0


def _full_bundle_wizard(ui: UI, workspace_root: Optional[Path] = None) -> int:
    """Build the large, genuinely offline bundle on a chosen mounted device."""
    import shutil

    from . import fullbundle, portable

    print()
    print(ui.paint("  Full offline USB bundle", "\033[1m"))
    print("  This downloads portable Python, llama.cpp and a GGUF model once,")
    print("  then writes a complete bundle to the mounted device you choose.")
    devices = portable.connected_devices()
    if devices:
        print("  mounted device directories:")
        for index, device in enumerate(devices, 1):
            print(f"    {index}. {device.path}  ({device.label})")
    else:
        print("  no mounted device directories found; enter the mount path directly")
    raw = input("  device number or mounted USB directory: ").strip()
    if not raw:
        ui.error("no target given")
        return 2
    try:
        if raw.isdigit():
            index = int(raw)
            if not 1 <= index <= len(devices):
                ui.error(f"no device number {index}; choose 1-{len(devices)}")
                return 2
            device = portable._validate_target(devices[index - 1].path)
        else:
            device = portable._validate_target(Path(raw))
    except portable.PortableError as exc:
        ui.error(str(exc))
        return 1
    if workspace_root is not None:
        workspace = Path(workspace_root).expanduser().resolve()
        if device == workspace or workspace in device.parents:
            ui.error("refusing to write the full bundle inside this workspace")
            return 1
    folder = input("  folder on the device [offgrid-usb]: ").strip() or "offgrid-usb"
    if folder in (".", "..") or Path(folder).name != folder:
        ui.error("folder must be a single directory name on the selected device")
        return 2
    destination = device / folder
    if destination.exists():
        ui.error(f"{destination} already exists; choose another folder")
        return 1
    model = input("  model [7b] (3b/7b/14b): ").strip().lower() or "7b"
    if model not in fullbundle.MODELS:
        ui.error(f"unknown model '{model}'; choose from: {', '.join(fullbundle.MODELS)}")
        return 2
    needed = fullbundle.estimate_download_gb(model)
    free = shutil.disk_usage(device).free / (1024 ** 3)
    if free < needed:
        ui.error(
            f"not enough free space on {device}: need about {needed:.1f} GB, "
            f"have {free:.1f} GB"
        )
        return 1
    print()
    print(f"  build     {destination}")
    print(f"  model     {model} (~{fullbundle.MODELS[model]['gb']} GB download)")
    print(f"  download  about {needed:.1f} GB total; this can take a while")
    print("  This writes into mounted storage. It is not a raw block-device flash.")
    try:
        answer = input("  type 'build' to continue: ").strip().lower()
    except EOFError:
        return 1
    if answer != "build":
        ui.info("cancelled")
        return 0
    try:
        result = fullbundle.build_full_bundle(destination, model=model)
    except (fullbundle.FullBundleError, OSError, SystemExit) as exc:
        ui.error(str(exc))
        return 1
    ui.ok(f"full bundle: {result.target}")
    ui.ok(f"model: {result.model_file}")
    print(f"  start it with: {result.target / 'launch' / 'offgrid.sh'} chat")
    return 0


def _distribution_wizard(ui: UI, workspace_root: Optional[Path] = None) -> int:
    from . import portable

    print()
    print(ui.paint("  OffGrid distribution wizard", "\033[1m"))
    print("  1  Build a portable zip and single-file .pyz")
    print("  2  Install a bundle on a mounted device")
    print("  3  List mounted device directories")
    print("  4  Build a full offline USB bundle (portable Python, llama.cpp, model)")
    print("  q  Quit")
    try:
        choice = input("  choice [1-4, q]: ").strip().lower()
    except EOFError:
        return 1
    if choice in ("q", "quit", ""):
        return 0

    if choice == "4":
        return _full_bundle_wizard(ui, workspace_root)

    if choice == "3":
        devices = portable.connected_devices()
        if not devices:
            ui.warn("no mounted device directories found; pass the mount path directly with --to")
        for device in devices:
            print(f"  {device.path}  ({device.label})")
        return 0

    if choice == "1":
        raw = input(f"  output zip [{portable.DEFAULT_BUNDLE}]: ").strip()
        destination = Path(raw) if raw else portable.DEFAULT_BUNDLE
        with_tests = input("  include tests? [y/N]: ").strip().lower() in ("y", "yes")
        try:
            zip_path, pyz_path = portable.build_distribution(
                destination, with_tests=with_tests, force=False
            )
        except (portable.PortableError, OSError) as exc:
            ui.error(str(exc))
            return 1
        ui.ok(f"portable zip: {zip_path}")
        ui.ok(f"single-file app: {pyz_path}")
        return 0

    if choice == "2":
        devices = portable.connected_devices()
        if devices:
            print("  mounted devices:")
            for index, device in enumerate(devices, 1):
                print(f"    {index}. {device.path}  ({device.label})")
        target = input("  mounted device directory: ").strip()
        if not target:
            ui.error("no target given")
            return 2
        default = portable.find_default_bundle() or portable.DEFAULT_BUNDLE
        raw_bundle = input(f"  bundle [{default}]: ").strip()
        bundle = Path(raw_bundle) if raw_bundle else default
        print(f"  install {bundle} into {target}")
        print("  This copies files to mounted storage; it is not a raw disk flash.")
        try:
            answer = input("  type 'install' to continue: ").strip().lower()
        except EOFError:
            return 1
        if answer != "install":
            ui.info("cancelled")
            return 0
        try:
            destination = portable.install_bundle(bundle, Path(target))
        except (portable.PortableError, OSError) as exc:
            ui.error(str(exc))
            return 1
        ui.ok(f"installed to {destination}")
        return 0

    ui.error("unknown choice")
    return 2


def cmd_distribute(args: argparse.Namespace) -> int:
    config, ui, workspace = prepare(args)
    from . import portable

    del config
    if getattr(args, "wizard", False):
        return _distribution_wizard(ui, workspace.root)
    if not any(
        (
            getattr(args, "build", None) is not None,
            getattr(args, "list_devices", False),
            bool(getattr(args, "to", "")),
        )
    ):
        return _distribution_wizard(ui, workspace.root)

    payload: Dict[str, Any] = {}
    built_zip: Optional[Path] = None
    try:
        if getattr(args, "build", None) is not None:
            destination = Path(args.build) if args.build else portable.DEFAULT_BUNDLE
            zip_path, pyz_path = portable.build_distribution(
                destination, with_tests=args.with_tests, force=args.force
            )
            built_zip = zip_path
            payload["built"] = {"zip": str(zip_path), "pyz": str(pyz_path)}
            if not args.json:
                ui.ok(f"portable zip: {zip_path}")
                ui.ok(f"single-file app: {pyz_path}")

        if getattr(args, "list_devices", False):
            devices = portable.connected_devices()
            payload["devices"] = [device.as_dict() for device in devices]
            if not args.json:
                if not devices:
                    ui.info("no mounted device directories found")
                for device in devices:
                    print(f"  {device.path}  ({device.label})")

        if getattr(args, "to", ""):
            bundle = Path(args.bundle) if args.bundle else (built_zip or portable.find_default_bundle())
            if bundle is None:
                ui.error("no bundle found; pass --bundle PATH or build one with --build")
                return 1
            if not args.dry_run and not args.yes:
                if not sys.stdin.isatty():
                    ui.error("refusing to install without confirmation; pass --yes")
                    return 2
                print(f"  copy {bundle} to {args.to}")
                print("  This writes into mounted storage. It is not a raw block-device flash.")
                answer = input("  type 'install' to continue: ").strip().lower()
                if answer != "install":
                    ui.info("cancelled")
                    return 0
            destination = portable.install_bundle(
                bundle, Path(args.to), force=args.force, dry_run=args.dry_run
            )
            payload["installed"] = str(destination)
            if not args.json:
                verb = "would install" if args.dry_run else "installed"
                ui.ok(f"{verb} to {destination}")
    except (portable.PortableError, OSError) as exc:
        ui.error(str(exc))
        return 1

    if args.json:
        print(json.dumps(payload, indent=2))
    return 0


def cmd_trust(args: argparse.Namespace) -> int:
    config, ui, workspace = prepare(args)
    from .audit import history
    from .trust import TRUST, posture, summary_line

    print()
    print(ui.paint("  what this software promises", "\033[1m"))
    for line in TRUST.splitlines():
        print("  " + line)
    print()
    print(ui.paint("  what it may do right now", "\033[1m"))
    for line in posture(config):
        print("  " + line)
    entries = history(workspace.root / config.security_history, limit=1)
    if entries:
        entry = entries[0]
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.get("t", 0)))
        total = (entry.get("counts") or {}).get("total", 0)
        print(f"  last security review  {stamp} - {entry.get('verdict', '?')} ({total} finding(s))")
    else:
        print("  last security review  none recorded yet")
    print()
    for line in summary_line(config):
        print("  " + line)
    print()
    return 0


COMMANDS = {
    "run": cmd_run,
    "chat": cmd_chat,
    "doctor": cmd_doctor,
    "models": cmd_models,
    "agents": cmd_agents,
    "config": cmd_config,
    "memory": cmd_memory,
    "lexicon": cmd_lexicon,
    "library": cmd_library,
    "council": cmd_council,
    "hardware": cmd_hardware,
    "fleet": cmd_fleet,
    "security": cmd_security,
    "trust": cmd_trust,
    "protect": cmd_protect,
    "distribute": cmd_distribute,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        print(HELP)
        return 0
    handler = COMMANDS.get(args.command)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
