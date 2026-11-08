"""The council: privacy gates, fan-out, failure handling, disagreement."""

import unittest

from offgrid.config import Config
from offgrid.council import Council, CouncilUnavailable, looks_ignorant


def reply(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


class IgnoranceTests(unittest.TestCase):
    def test_plain_admissions_are_recognised(self):
        for text in (
            "I don't know which flag does that.",
            "I do not know the version.",
            "I cannot determine the answer from the repository.",
            "There is not enough information to say.",
            "I have no information about this API.",
            "It is unknown from this code.",
            "No documentation is available here.",
        ):
            self.assertTrue(looks_ignorant(text), text)

    def test_normal_answers_are_not_mistaken_for_ignorance(self):
        for text in (
            "I added the retry loop and the tests pass.",
            "The verify command is `make test`.",
            "I know this repository uses pytest.",
            "Fixed the bug in app.py.",
        ):
            self.assertFalse(looks_ignorant(text), text)


class CouncilTests(unittest.TestCase):
    def make(self, **overrides):
        config = Config(
            **{
                "endpoint": "http://127.0.0.1:8080/v1",
                "model": "local-model",
                "color": False,
                "verbose": 0,
                **overrides,
            }
        )
        calls = []

        def post(member, messages, timeout):
            calls.append((member.name, messages))
            if member.name.startswith("broken") or ":9/" in member.endpoint:
                raise RuntimeError("connection refused")
            return reply(f"answer from {member.name}")

        council = Council(config, post=post)
        return council, calls, config

    def test_disabled_by_default(self):
        council, _calls, _cfg = self.make()
        self.assertFalse(council.available())
        with self.assertRaises(CouncilUnavailable) as ctx:
            council.ask("what is a monad")
        self.assertIn("disabled", str(ctx.exception))

    def test_local_member_answers(self):
        council, calls, _cfg = self.make(council=True)
        answer = council.ask("what is a monad")
        self.assertTrue(answer.usable)
        self.assertEqual([item.member for item in answer.answers], ["local"])
        self.assertEqual(len(calls), 1)
        self.assertIn("what is a monad", calls[0][1][-1]["content"])

    def test_public_member_is_skipped_without_permission(self):
        council, _calls, _cfg = self.make(council=True, council_members=("local", "pollinations"))
        answer = council.ask("anything")
        self.assertEqual([item.member for item in answer.answers], ["local"])
        self.assertTrue(any("pollinations" in entry for entry in answer.skipped))

    def test_public_member_included_when_allowed(self):
        council, calls, _cfg = self.make(
            council=True,
            allow_public_council=True,
            council_members=("local", "pollinations"),
        )
        answer = council.ask("anything")
        names = [item.member for item in answer.answers]
        self.assertEqual(sorted(names), ["local", "pollinations"])
        self.assertEqual(len(calls), 2)

    def test_failing_member_does_not_break_the_others(self):
        council, _calls, _cfg = self.make(
            council=True,
            council_members=("local", "url:http://127.0.0.1:9/v1"),
        )
        answer = council.ask("anything")
        self.assertTrue(answer.usable)
        failed = [item for item in answer.answers if not item.ok]
        self.assertEqual(len(failed), 1)
        self.assertIn("RuntimeError", failed[0].error)

    def test_all_members_failing_is_reported_not_invented(self):
        council, _calls, _cfg = self.make(council=True, council_members=("url:http://127.0.0.1:9/v1",))
        answer = council.ask("anything")
        self.assertFalse(answer.usable)
        self.assertIn("Do not guess", answer.render())

    def test_context_is_truncated(self):
        council, calls, _cfg = self.make(council=True, council_max_chars=300)
        council.ask("question", context="x" * 5000)
        content = calls[0][1][-1]["content"]
        self.assertLessEqual(len(content), 340)
        self.assertIn("[truncated]", content)

    def test_disagreement_when_one_member_admits_ignorance(self):
        config = Config(
            endpoint="http://127.0.0.1:8080/v1",
            council=True,
            verbose=0,
            color=False,
            council_members=("local", "local-model:second"),
        )

        def post(member, messages, timeout):
            if member.name == "local":
                return reply("It is probably the flux capacitor.")
            return reply("I don't know, that needs the specific API version.")

        council = Council(config, post=post)
        answer = council.ask("what does this undocumented flag do")
        self.assertTrue(answer.disagreement)
        self.assertIn("do not agree", answer.render())

    def test_no_members_available(self):
        council, _calls, _cfg = self.make(council=True, council_members=("url:https://example.com/v1",))
        with self.assertRaises(CouncilUnavailable) as ctx:
            council.ask("hello")
        self.assertIn("no council members", str(ctx.exception))

    def test_ping_reports_skipped_public_members(self):
        council, _calls, _cfg = self.make(council=True, council_members=("local", "pollinations"))
        results = council.ping()
        names = [name for name, _ok, _note in results]
        self.assertTrue(any("pollinations" in name for name in names))
        self.assertTrue(any(ok for name, ok, _note in results if name == "local"))

    def test_labels_which_members_would_be_asked(self):
        council, _calls, _cfg = self.make(council=True)
        self.assertEqual(council.label(), "local")

    def test_unknown_spec_ignored(self):
        council, _calls, _cfg = self.make(council=True, council_members=("nonsense",))
        self.assertEqual(council.members(), [])


if __name__ == "__main__":
    unittest.main()
