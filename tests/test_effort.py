"""Effort profiles: quality first, then power, then speed."""

import unittest

from offgrid.config import Config
from offgrid.effort import ORDERING, PROFILES, override, resolve


class ProfileTests(unittest.TestCase):
    def test_best_is_the_default(self):
        self.assertEqual(Config().effort, "best")
        self.assertEqual(resolve(Config()).name, "best")

    def test_unknown_profile_never_degrades_to_fast(self):
        self.assertEqual(resolve(Config(effort="turbo")).name, "best")
        self.assertEqual(resolve(Config(effort="")).name, "best")

    def test_ordering_is_performance_power_speed(self):
        self.assertEqual(ORDERING, ("performance", "power", "speed"))
        self.assertEqual(PROFILES["best"].as_dict()["ordering"], ["performance", "power", "speed"])

    def test_best_deliberates_more_than_balanced_more_than_fast(self):
        best, balanced, fast = PROFILES["best"], PROFILES["balanced"], PROFILES["fast"]
        self.assertGreater(best.plan_candidates, balanced.plan_candidates)
        self.assertGreater(best.review_passes, balanced.review_passes)
        self.assertGreaterEqual(best.review_rounds, balanced.review_rounds)
        self.assertGreaterEqual(best.test_fix_rounds, balanced.test_fix_rounds)
        self.assertGreater(best.turn_multiplier, fast.turn_multiplier)
        self.assertTrue(best.quality_gate)
        self.assertFalse(fast.quality_gate)
        self.assertTrue(best.escalate)
        self.assertFalse(fast.escalate)

    def test_fast_is_cheapest_on_every_costly_dimension(self):
        fast = PROFILES["fast"]
        for profile in PROFILES.values():
            self.assertLessEqual(fast.plan_candidates, profile.plan_candidates)
            self.assertLessEqual(fast.review_passes, profile.review_passes)
            self.assertLessEqual(fast.quality_rounds, profile.quality_rounds)

    def test_explicit_override_wins_over_profile(self):
        self.assertEqual(override(0, 3), 0)
        self.assertEqual(override(1, 3), 1)
        self.assertEqual(override(-1, 3), 3)
        self.assertEqual(override(None, 3), 3)

    def test_every_profile_explains_itself(self):
        for name, profile in PROFILES.items():
            self.assertEqual(profile.name, name)
            self.assertTrue(profile.description)
            self.assertGreater(profile.max_tokens, 0)


if __name__ == "__main__":
    unittest.main()
