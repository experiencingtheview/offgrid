"""Custom agents: a workspace file extends the fixed six-role roster."""

import json
import tempfile
import unittest
from pathlib import Path

from offgrid import agents
from offgrid.agents import ALL_TOOLS, ROSTER, load_custom_agents


class CustomAgentLoadingTests(unittest.TestCase):
    def load(self, payload):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agents.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_custom_agents(path)

    def test_a_workspace_file_adds_agents_with_their_own_prompt_and_tools(self):
        specs, warnings = self.load(
            {
                "agents": [
                    {
                        "key": "security",
                        "title": "who audits the change for secrets",
                        "prompt": "You are security. Read the diff and report findings.",
                        "tools": ["read_file", "git_diff", "finish"],
                        "max_turns": 6,
                    }
                ]
            }
        )
        self.assertEqual(warnings, [])
        spec = specs["security"]
        self.assertEqual(spec.title, "who audits the change for secrets")
        self.assertIn("Read the diff", spec.body)
        self.assertEqual(spec.tools, ("read_file", "git_diff", "finish"))
        self.assertEqual(spec.max_turns, 6)
        self.assertNotIn("security", ROSTER)

    def test_a_builtin_role_can_be_cloned_for_another_worker(self):
        specs, warnings = self.load({"agents": [{"key": "coder2", "like": "coder"}]})
        self.assertEqual(warnings, [])
        spec = specs["coder2"]
        self.assertEqual(spec.name, "coder")
        self.assertEqual(spec.body, ROSTER["coder"].body)
        self.assertEqual(spec.tools, ALL_TOOLS)
        self.assertEqual(spec.max_turns, ROSTER["coder"].max_turns)

    def test_builtin_keys_and_alias_keys_cannot_be_replaced(self):
        specs, warnings = self.load(
            {
                "agents": [
                    {"key": "coder", "title": "x", "prompt": "y"},
                    {"key": "writer", "title": "x", "prompt": "y"},
                ]
            }
        )
        self.assertEqual(specs, {})
        self.assertEqual(len(warnings), 2)
        self.assertIn("built-in", warnings[0])
        self.assertIn("built-in", warnings[1])

    def test_unknown_tools_and_bad_turn_counts_are_refused(self):
        specs, warnings = self.load(
            {
                "agents": [
                    {"key": "sneaky", "title": "x", "prompt": "y", "tools": ["sudo"]},
                    {"key": "slow", "title": "x", "prompt": "y", "max_turns": 99},
                ]
            }
        )
        self.assertEqual(specs, {})
        self.assertEqual(len(warnings), 2)
        self.assertIn("sudo", warnings[0])
        self.assertIn("max_turns", warnings[1])

    def test_a_missing_file_is_not_an_error_and_malformed_json_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "agents.json"
            self.assertEqual(load_custom_agents(missing), ({}, []))
            missing.write_text("{not json", encoding="utf-8")
            specs, warnings = load_custom_agents(missing)
            self.assertEqual(specs, {})
            self.assertEqual(len(warnings), 1)
            self.assertIn("custom agents ignored", warnings[0])

    def test_describe_roster_marks_the_extra_agents(self):
        specs, _ = self.load(
            {"agents": [{"key": "security", "title": "who audits", "prompt": "p"}]}
        )
        text = agents.describe_roster(specs)
        self.assertIn("security", text)
        self.assertIn("(custom)", text)

    def test_a_cloned_role_shows_the_key_to_assign(self):
        specs, _ = self.load({"agents": [{"key": "coder2", "like": "coder"}]})
        text = agents.describe_roster(specs)
        self.assertIn("(custom: coder2)", text)


if __name__ == "__main__":
    unittest.main()
