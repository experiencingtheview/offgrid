"""How hard the team tries before it answers.

The ordering is fixed and deliberate:

    1. performance - does the result actually work, edge cases included
    2. power       - use the strongest means available before answering
    3. speed       - last, always last

`best` is the default. Nothing here trades correctness for latency: the fast
profile exists so tests and throwaway edits do not pay for deliberation, not so
answers get worse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .config import Config

ORDERING = ("performance", "power", "speed")


@dataclass(frozen=True)
class Effort:
    name: str
    description: str
    plan_candidates: int
    plan_critique: bool
    review_passes: int
    review_rounds: int
    test_fix_rounds: int
    quality_gate: bool
    quality_rounds: int
    turn_multiplier: float
    max_tokens: int
    escalate: bool

    def as_dict(self) -> Dict[str, object]:
        return {
            "profile": self.name,
            "description": self.description,
            "plan_candidates": self.plan_candidates,
            "plan_critique": self.plan_critique,
            "review_passes": self.review_passes,
            "review_rounds": self.review_rounds,
            "test_fix_rounds": self.test_fix_rounds,
            "quality_gate": self.quality_gate,
            "quality_rounds": self.quality_rounds,
            "turn_multiplier": self.turn_multiplier,
            "max_tokens": self.max_tokens,
            "escalate": self.escalate,
            "ordering": list(ORDERING),
        }


PROFILES: Dict[str, Effort] = {
    "best": Effort(
        name="best",
        description="draft several plans, pick the strongest, catch mechanical defects, review twice, escalate on failure",
        plan_candidates=2,
        plan_critique=True,
        review_passes=2,
        review_rounds=3,
        test_fix_rounds=3,
        quality_gate=True,
        quality_rounds=2,
        turn_multiplier=1.5,
        max_tokens=3072,
        escalate=True,
    ),
    "balanced": Effort(
        name="balanced",
        description="one plan, one review, mechanical checks, escalation on failure",
        plan_candidates=1,
        plan_critique=False,
        review_passes=1,
        review_rounds=2,
        test_fix_rounds=2,
        quality_gate=True,
        quality_rounds=1,
        turn_multiplier=1.0,
        max_tokens=2048,
        escalate=True,
    ),
    "fast": Effort(
        name="fast",
        description="one pass, no deliberation; for tests and throwaway edits, not for real answers",
        plan_candidates=1,
        plan_critique=False,
        review_passes=1,
        review_rounds=1,
        test_fix_rounds=1,
        quality_gate=False,
        quality_rounds=0,
        turn_multiplier=1.0,
        max_tokens=1536,
        escalate=False,
    ),
}

DEFAULT = "best"


def resolve(config: Config) -> Effort:
    """Unknown profile names fall back to `best`, never to `fast`."""
    name = (config.effort or DEFAULT).strip().lower()
    return PROFILES.get(name, PROFILES[DEFAULT])


def override(config: Optional[int], profile_value: int) -> int:
    """Explicit config wins; -1 means 'use the profile'."""
    if config is None or config < 0:
        return profile_value
    return config
