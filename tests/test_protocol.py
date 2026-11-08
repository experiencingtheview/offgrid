"""The text tool protocol, used by models without native function calling."""

import unittest

from offgrid.protocol import parse_text_tool_calls, strip_tool_blocks

ALLOWED = ("read_file", "write_file", "finish")


class ParseTests(unittest.TestCase):
    def test_reads_fenced_tool_block(self):
        reply = 'Let me look.\n```tool\n{"tool": "read_file", "args": {"path": "app.py"}}\n```'
        calls = parse_text_tool_calls(reply, ALLOWED)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "read_file")
        self.assertEqual(calls[0].arguments, {"path": "app.py"})

    def test_accepts_arguments_as_json_string(self):
        reply = '```tool\n{"tool": "write_file", "args": "{\\"path\\": \\"a.py\\"}"}\n```'
        calls = parse_text_tool_calls(reply, ALLOWED)
        self.assertEqual(calls[0].arguments, {"path": "a.py"})

    def test_accepts_bare_fence(self):
        reply = '```\n{"name": "finish", "arguments": {"summary": "done"}}\n```'
        calls = parse_text_tool_calls(reply, ALLOWED)
        self.assertEqual(calls[0].name, "finish")
        self.assertEqual(calls[0].arguments, {"summary": "done"})

    def test_ignores_unknown_tools(self):
        reply = '```tool\n{"tool": "sudo_rm_rf", "args": {}}\n```'
        self.assertEqual(parse_text_tool_calls(reply, ALLOWED), [])

    def test_ignores_ordinary_code_blocks(self):
        reply = "Here is the fix:\n```python\nprint('hello')\n```\n"
        self.assertEqual(parse_text_tool_calls(reply, ALLOWED), [])

    def test_ignores_malformed_json(self):
        reply = '```tool\n{"tool": "read_file", "args": \n```'
        self.assertEqual(parse_text_tool_calls(reply, ALLOWED), [])

    def test_strip_keeps_prose(self):
        reply = 'Working on it.\n```tool\n{"tool": "read_file", "args": {"path": "x"}}\n```\nDone soon.'
        cleaned = strip_tool_blocks(reply)
        self.assertIn("Working on it.", cleaned)
        self.assertIn("Done soon.", cleaned)
        self.assertNotIn("read_file", cleaned)


if __name__ == "__main__":
    unittest.main()
