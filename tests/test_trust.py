import unittest

from offgrid.agents import get_agent
from offgrid.config import Config
from offgrid.orchestrator import RunResult
from offgrid.prompt import HONESTY, TRUST, system_prompt
from offgrid.trust import disclosures, posture


def built(**overrides):
    result = RunResult(task="add a flag")
    result.verify_ok = True
    result.verify_command = "python3 -m pytest -q"
    result.review_verdict = "approve"
    result.effort = "best"
    result.hardware = "standard"
    result.capacity = "full"
    for key, value in overrides.items():
        setattr(result, key, value)
    return result


class DisclosureTests(unittest.TestCase):
    def test_verified_run_says_so(self):
        lines = disclosures(built(), Config())
        self.assertTrue(any(line.startswith("Verified:") for line in lines))

    def test_unverified_run_does_not_overclaim(self):
        result = built(verify_ok=None, verify_command="")
        lines = disclosures(result, Config())
        self.assertTrue(any(line.startswith("Not verified:") and "no command" in line for line in lines))

    def test_failed_verification_is_not_hidden(self):
        result = built(verify_ok=False, verify_command="false")
        lines = disclosures(result, Config())
        self.assertTrue(any("failed" in line and "unfinished" in line for line in lines))

    def test_council_is_called_an_outside_opinion(self):
        result = built(council=[{"agent": "coder", "members": ["local"], "answer": "try --jinja"}])
        lines = disclosures(result, Config(council=True))
        self.assertTrue(any("outside opinions" in line for line in lines))
        self.assertFalse(any("left this machine" in line for line in lines))

    def test_public_council_is_disclosed(self):
        result = built(council=[{"agent": "coder", "members": ["pollinations"], "answer": "x"}])
        config = Config(council=True, allow_public_council=True)
        lines = disclosures(result, config)
        self.assertTrue(any("left this machine" in line for line in lines))

    def test_security_findings_are_reported(self):
        result = built(security=[{"severity": "critical", "check": "secret"}])
        lines = disclosures(result, Config())
        self.assertTrue(any("Security:" in line and "critical" in line for line in lines))

    def test_uncommitted_work_is_stated(self):
        result = built(changed=["app.py", "tests/test_app.py"])
        lines = disclosures(result, Config())
        self.assertTrue(any("Nothing was committed" in line for line in lines))

    def test_escalation_that_still_fails_is_stated(self):
        result = built(escalated=True, verify_ok=False, verify_command="false")
        lines = disclosures(result, Config())
        self.assertTrue(any("strongest attempt still fails" in line for line in lines))


class PostureTests(unittest.TestCase):
    def test_default_posture_is_local_and_private(self):
        text = "\n".join(posture(Config(workspace="/tmp/project")))
        self.assertIn("(on this machine)", text)
        self.assertIn("council        off", text)
        self.assertIn("pending lessons", text)

    def test_public_council_posture_is_not_buried(self):
        text = "\n".join(posture(Config(allow_public_council=True, council=True)))
        self.assertIn("prompts may leave this machine", text)


class PromptTests(unittest.TestCase):
    def test_trust_is_in_every_prompt_and_rules_stay_last(self):
        body = system_prompt("coder", "the implementer", "BODY")
        self.assertIn(TRUST.strip().splitlines()[0], body)
        self.assertIn("this is software, not a person", body)
        self.assertNotIn(HONESTY, body)  # the agent appends it after its tool protocol
        full = get_agent("coder").system()
        self.assertIn(HONESTY.strip(), full)
        self.assertTrue(full.rstrip().endswith(HONESTY.strip()[-40:]))


if __name__ == "__main__":
    unittest.main()
