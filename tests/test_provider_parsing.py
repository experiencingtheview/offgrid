"""Models that answer with a fenced tool block even when native tools were offered."""

import unittest

from offgrid.config import Config
from offgrid.providers import LocalProvider


def reply(content):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {},
    }


class FencedFallbackTests(unittest.TestCase):
    def provider(self):
        return LocalProvider(Config(endpoint="http://127.0.0.1:8080/v1"))

    def test_fenced_block_is_parsed_when_native_tools_were_offered(self):
        content = '```tool\n{"name": "write_file", "arguments": {"path": "textutil.py"}}\n```'
        completion = self.provider()._parse(
            reply(content), text_protocol=False, tool_names=("write_file",), started=0.0
        )
        self.assertEqual(len(completion.tool_calls), 1)
        self.assertEqual(completion.tool_calls[0].name, "write_file")
        self.assertEqual(completion.tool_calls[0].arguments, {"path": "textutil.py"})
        self.assertEqual(completion.text, "")

    def test_ordinary_code_block_is_still_not_a_tool_call(self):
        content = "Here is the fix:\n```python\nprint('hello')\n```"
        completion = self.provider()._parse(
            reply(content), text_protocol=False, tool_names=("write_file",), started=0.0
        )
        self.assertEqual(completion.tool_calls, [])
        self.assertIn("print('hello')", completion.text)


if __name__ == "__main__":
    unittest.main()
