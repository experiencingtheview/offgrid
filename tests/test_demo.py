import unittest

from offgrid.demo import run_demo


class DemoTests(unittest.TestCase):
    def test_scripted_demo_runs_the_whole_team_without_a_model(self):
        self.assertEqual(run_demo(color=False), 0)


if __name__ == "__main__":
    unittest.main()
