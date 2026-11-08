"""Effort is the intent; hardware sets the ceiling it gets spent against."""

import unittest

from offgrid.effort import PROFILES
from offgrid.hardware import Capability
from offgrid.scale import CAPACITIES, adjust, capacity_for, describe


def capability(tier):
    return Capability(os_name="Linux", machine="x86_64", cpu_count=8, ram_gb=16, tier=tier)


class CapacityTests(unittest.TestCase):
    def test_unknown_hardware_gets_the_middle(self):
        self.assertEqual(capacity_for(None).name, "modest")

    def test_each_tier_has_a_capacity(self):
        for tier in ("minimal", "modest", "capable", "workstation"):
            self.assertEqual(capacity_for(capability(tier)).name, tier)

    def test_bigger_machines_get_more_context_and_parallelism(self):
        minimal, modest, capable, workstation = (
            CAPACITIES[name] for name in ("minimal", "modest", "capable", "workstation")
        )
        self.assertLess(minimal.context_chars, modest.context_chars)
        self.assertLess(modest.context_chars, capable.context_chars)
        self.assertLess(capable.context_chars, workstation.context_chars)
        self.assertLessEqual(minimal.parallel_steps, workstation.parallel_steps)

    def test_capacity_explains_itself(self):
        for capacity in CAPACITIES.values():
            self.assertTrue(capacity.note)


class AdjustTests(unittest.TestCase):
    def test_fast_is_never_quietly_upgraded(self):
        fast = PROFILES["fast"]
        self.assertIs(adjust(fast, CAPACITIES["workstation"]), fast)
        self.assertIs(adjust(fast, CAPACITIES["minimal"]), fast)

    def test_workstation_gets_more_attempts(self):
        best = PROFILES["best"]
        scaled = adjust(best, CAPACITIES["workstation"])
        self.assertGreater(scaled.plan_candidates, best.plan_candidates)
        self.assertGreaterEqual(scaled.test_fix_rounds, best.test_fix_rounds)
        self.assertGreaterEqual(scaled.max_tokens, 4096)
        self.assertGreaterEqual(scaled.turn_multiplier, 2.0)
        self.assertEqual(scaled.review_passes, best.review_passes)  # rigour, not repetition
        self.assertTrue(scaled.quality_gate)

    def test_capable_machine_gets_deeper_turns(self):
        scaled = adjust(PROFILES["best"], CAPACITIES["capable"])
        self.assertGreaterEqual(scaled.max_tokens, 3072)
        self.assertGreaterEqual(scaled.turn_multiplier, 1.75)
        self.assertTrue(scaled.quality_gate)

    def test_small_machine_narrows_breadth_not_checks(self):
        scaled = adjust(PROFILES["best"], CAPACITIES["minimal"])
        self.assertEqual(scaled.plan_candidates, 1)
        self.assertFalse(scaled.plan_critique)
        self.assertEqual(scaled.review_passes, 1)
        self.assertLessEqual(scaled.max_tokens, 1024)
        # the deterministic checks survive: they are what makes weak hardware usable
        self.assertTrue(scaled.quality_gate)
        self.assertGreaterEqual(scaled.quality_rounds, 1)
        self.assertTrue(scaled.escalate)

    def test_modest_hardware_leaves_the_profile_alone(self):
        self.assertIs(adjust(PROFILES["best"], CAPACITIES["modest"]), PROFILES["best"])

    def test_describe_says_what_will_happen(self):
        text = describe(CAPACITIES["workstation"], adjust(PROFILES["best"], CAPACITIES["workstation"]))
        self.assertIn("workstation", text)
        self.assertIn("plan(s)", text)


if __name__ == "__main__":
    unittest.main()
