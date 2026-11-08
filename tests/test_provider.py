"""The local model client: loopback guard, health, tool calls, text fallback."""

import unittest

from offgrid.config import Config
from offgrid.models import Message
from offgrid.providers import LocalProvider, ProviderError, is_loopback
from tests.fake_llm import serve, text, tool

READ_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "read",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }
]


class LoopbackGuardTests(unittest.TestCase):
    def test_loopback_detection(self):
        self.assertTrue(is_loopback("http://127.0.0.1:8080/v1"))
        self.assertTrue(is_loopback("http://localhost:11434/v1"))
        self.assertFalse(is_loopback("https://api.example.com/v1"))

    def test_remote_endpoint_refused_by_default(self):
        config = Config(endpoint="https://api.openai.com/v1", model="gpt-4")
        with self.assertRaises(ProviderError) as ctx:
            LocalProvider(config)
        self.assertIn("offline", str(ctx.exception))

    def test_remote_endpoint_allowed_when_opted_in(self):
        config = Config(endpoint="https://api.example.com/v1", allow_remote=True)
        self.assertEqual(LocalProvider(config).endpoint, "https://api.example.com/v1")


class ServerTests(unittest.TestCase):
    def test_models_and_health(self):
        model = serve([])
        self.addCleanup(model.stop)
        provider = LocalProvider(Config(endpoint=model.url))
        self.assertEqual(provider.models(), ["fake-coder-7b"])
        ok, message = provider.health()
        self.assertTrue(ok)
        self.assertIn("fake-coder-7b", message)
        self.assertEqual(provider.model, "fake-coder-7b")

    def test_health_reports_unreachable_server(self):
        provider = LocalProvider(Config(endpoint="http://127.0.0.1:9/v1", request_timeout=1))
        ok, message = provider.health()
        self.assertFalse(ok)
        self.assertIn("cannot reach", message)

    def test_native_tool_call_parsed(self):
        model = serve([(lambda body: True, tool("read_file", path="a.py"))])
        self.addCleanup(model.stop)
        provider = LocalProvider(Config(endpoint=model.url))
        completion = provider.chat(
            [Message("system", "You are coder"), Message("user", "look at a.py")],
            tools=READ_SCHEMA,
            tool_names=["read_file"],
        )
        self.assertEqual(len(completion.tool_calls), 1)
        self.assertEqual(completion.tool_calls[0].name, "read_file")
        self.assertEqual(completion.tool_calls[0].arguments, {"path": "a.py"})
        self.assertTrue(completion.prompt_tokens > 0)

    def test_falls_back_to_text_protocol(self):
        reply = 'Reading it.\n```tool\n{"tool": "read_file", "args": {"path": "a.py"}}\n```'
        model = serve([(lambda body: True, text(reply))], reject_tools=True)
        self.addCleanup(model.stop)
        provider = LocalProvider(Config(endpoint=model.url))
        completion = provider.chat(
            [Message("system", "You are coder"), Message("user", "look at a.py")],
            tools=READ_SCHEMA,
            tool_names=["read_file"],
        )
        self.assertFalse(provider.native_tools)
        self.assertEqual(len(completion.tool_calls), 1)
        self.assertEqual(completion.tool_calls[0].name, "read_file")
        self.assertNotIn("```", completion.text)
        # The retry must have dropped the tools array on the second request.
        self.assertIn("tools", model.requests[0])
        self.assertNotIn("tools", model.requests[1])

    def test_plain_text_reply(self):
        model = serve([(lambda body: True, text("nothing to do here"))])
        self.addCleanup(model.stop)
        provider = LocalProvider(Config(endpoint=model.url))
        completion = provider.chat([Message("user", "hi")])
        self.assertEqual(completion.text, "nothing to do here")
        self.assertEqual(completion.tool_calls, [])


if __name__ == "__main__":
    unittest.main()
