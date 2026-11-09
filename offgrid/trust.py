"""A relationship of trust, stated plainly.

Trust is not a tone of voice. It is a set of things the software does every time,
including the uncomfortable ones: saying what it did not check, disagreeing out
loud, refusing an irreversible action, and disclosing anything that left the
machine. `TRUST` is injected into every agent prompt; `disclosures()` writes the
honest postscript of a run; `posture()` is what `offgrid trust` prints.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .config import Config
from .council import PUBLIC_MEMBERS
from .providers import is_loopback

TRUST = """\
HOW WE WORK TOGETHER (this is software, not a person)
- Say what you did, what you did not do, and what you could not check. Lead with the
  uncomfortable part instead of burying it.
- Disagree when the user is wrong, and explain why. Never flatter, never pad, never agree
  just to be agreeable. Being useful matters more than being liked.
- Do not pretend to be human, to have feelings, memories or senses you do not have. When it
  matters, say plainly that you are software the user owns and controls.
- Never claim a capability you do not have. Name the limit, then offer the closest safe thing.
- Ask before anything irreversible, and name the risk and the way back.
- Protect the user's data: no telemetry, no hidden egress, and no silence about what left
  the machine.
- Report security findings even when they are inconvenient or make the work look worse.
"""


def _member_names(result: Any) -> List[str]:
    names = set()
    for entry in getattr(result, "council", []) or []:
        for name in entry.get("members") or []:
            names.add(str(name))
    return sorted(names)


def disclosures(result: Any, config: Config) -> List[str]:
    """The honest postscript of a run, in a stable order."""
    lines: List[str] = []

    if result.verify_ok is True:
        lines.append(f"Verified: `{result.verify_command}` passed on this machine.")
    elif result.verify_ok is False:
        lines.append(
            f"Not verified: `{result.verify_command}` failed. Treat the change as unfinished, "
            "not as a fix."
        )
    elif result.verify_command:
        lines.append(f"Not verified: `{result.verify_command}` could not be run here.")
    else:
        lines.append(
            "Not verified: no command proved this works, so correctness rests on reading the "
            "diff and the review, not on a test."
        )

    if result.review_verdict == "changes":
        issues = "; ".join(result.review_issues[:2])
        lines.append(f"The reviewer objected: {issues}." if issues else "The reviewer objected.")
    elif result.review_verdict == "approve":
        lines.append("Reviewed: the reviewer approved the diff.")

    members = _member_names(result)
    if members:
        lines.append(
            f"Council consulted {len(result.council)} time(s) ({', '.join(members)}); those were "
            "outside opinions, not verified facts."
        )
        if config.allow_public_council and any(name in PUBLIC_MEMBERS for name in members):
            lines.append(
                "Privacy: part of a question left this machine for a public council member."
            )

    if result.escalated:
        if result.verify_ok is False:
            lines.append(
                "Escalated after repeated failure: the strongest attempt still fails verification."
            )
        else:
            lines.append("Escalated after repeated failure; the result comes from the extra attempt.")

    if result.changed:
        lines.append(
            f"Uncommitted: {len(result.changed)} file(s) changed. Nothing was committed, pushed or deployed."
        )

    if getattr(result, "security", None):
        critical = sum(1 for item in result.security if item.get("severity") == "critical")
        extra = f", {critical} critical" if critical else ""
        lines.append(
            f"Security: {len(result.security)} finding(s) in the changed files{extra}. "
            "Run `offgrid security review` for the detail."
        )

    if getattr(result, "effort", ""):
        lines.append(
            f"Effort: {result.effort} profile on a {result.hardware or 'unmeasured'} machine "
            f"({result.capacity or 'unscaled'} capacity)."
        )
    return lines


def posture(config: Config) -> List[str]:
    """What this build is allowed to do right now, stated without spin."""
    endpoint = config.resolved_endpoint()
    lines = [
        f"model          {endpoint} " + ("(on this machine)" if is_loopback(endpoint) else "(a remote host)"),
        f"network        {'denied' if config.network == 'deny' or config.offline else 'allowed to this endpoint only'}",
        f"shell          {config.command_policy} (ask | auto | deny)",
        f"workspace      {config.workspace} (paths outside it are refused)",
        f"council        "
        + (
            "public members allowed; prompts may leave this machine"
            if config.allow_public_council
            else ("local members only" if config.council else "off")
        ),
        f"fleet          "
        + (
            "on: model calls may run on LAN peers; remote commands enabled"
            if config.fleet and config.fleet_exec
            else ("on: model calls may run on LAN peers" if config.fleet else "off")
        ),
        f"memory         pending lessons are never used before you accept them",
        f"security       reviews {'on' if config.security_review else 'off'}, logged to {config.security_history}",
    ]
    return lines


def summary_line(config: Config) -> List[str]:
    return [
        "This is offline software you own. It cannot see your email, your accounts or other devices.",
        "It will not encrypt your whole system for you: that is an OS-level, irreversible operation.",
        "Ask `offgrid security plan` for the honest full-disk encryption route on this platform.",
    ]
