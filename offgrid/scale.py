"""Turning measured hardware into the amount of work the team attempts.

The effort profile is the user's intent ("best answer, take your time"). This
module is the machine's side of the bargain: on a workstation the same intent
buys more candidate plans and more repair rounds, and on a 4 GB tablet it buys
fewer simultaneous attempts rather than fewer *checks*.

What never shrinks with hardware:

- the mechanical quality gate (it is deterministic and costs almost nothing),
- the reviewer,
- the honesty rules,
- verification.

What does shrink on a small machine: how many plans get drafted in parallel,
how much context is pasted into a prompt, and how many tool turns a single agent
gets. That is a breadth limit, not a correctness one - and it is stated plainly
in `describe()` instead of being a silent downgrade.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, Optional

from .effort import Effort
from .hardware import Capability


@dataclass(frozen=True)
class Capacity:
    name: str
    context_chars: int
    parallel_steps: int
    note: str

    def as_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "context_chars": self.context_chars,
            "parallel_steps": self.parallel_steps,
            "note": self.note,
        }


CAPACITIES: Dict[str, Capacity] = {
    "minimal": Capacity(
        name="minimal",
        context_chars=6_000,
        parallel_steps=1,
        note="one attempt at a time, short prompts; every check still runs",
    ),
    "modest": Capacity(
        name="modest",
        context_chars=14_000,
        parallel_steps=1,
        note="full pipeline, moderate context",
    ),
    "capable": Capacity(
        name="capable",
        context_chars=28_000,
        parallel_steps=2,
        note="longer context, independent steps can run together",
    ),
    "workstation": Capacity(
        name="workstation",
        context_chars=60_000,
        parallel_steps=4,
        note="large context, more candidate plans, several steps at once",
    ),
}

DEFAULT = CAPACITIES["modest"]


def capacity_for(capability: Optional[Capability]) -> Capacity:
    if capability is None:
        return DEFAULT
    return CAPACITIES.get(capability.tier, DEFAULT)


def adjust(effort: Effort, capacity: Capacity) -> Effort:
    """Raise the ceiling on strong hardware, narrow it on weak hardware.

    `fast` is left exactly as the user set it: an explicit request for a cheap
    run should not quietly become an expensive one on a big machine.
    """
    if effort.name == "fast":
        return effort

    if capacity.name == "workstation":
        return replace(
            effort,
            plan_candidates=effort.plan_candidates + 1,
            test_fix_rounds=effort.test_fix_rounds + 1,
            quality_rounds=max(effort.quality_rounds, 3),
            max_tokens=max(effort.max_tokens, 4096),
            turn_multiplier=max(effort.turn_multiplier, 2.0),
            description=effort.description + "; scaled up for this machine",
        )

    if capacity.name == "capable":
        return replace(
            effort,
            max_tokens=max(effort.max_tokens, 3072),
            turn_multiplier=max(effort.turn_multiplier, 1.75),
            description=effort.description + "; scaled for a capable machine",
        )

    if capacity.name == "minimal":
        return replace(
            effort,
            plan_candidates=1,
            plan_critique=False,
            review_passes=1,
            test_fix_rounds=min(effort.test_fix_rounds, 2),
            quality_rounds=min(effort.quality_rounds, 1),
            max_tokens=min(effort.max_tokens, 1024),
            turn_multiplier=min(effort.turn_multiplier, 1.0),
            description=effort.description + "; narrowed for a small machine",
        )

    return effort


def describe(capacity: Capacity, effort: Effort) -> str:
    return (
        f"{capacity.name}: {capacity.note}; "
        f"{effort.plan_candidates} plan(s), {effort.review_passes} review pass(es), "
        f"{effort.test_fix_rounds} fix round(s), {capacity.context_chars} chars of context"
    )
