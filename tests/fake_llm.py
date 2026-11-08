"""Compatibility import for the scripted model server, now shipped in the package.

The demo and the installed wheel need the same server as the tests. Keeping
this small re-export means existing test imports continue to work.
"""

from offgrid.fake_llm import FakeModel, serve, text, tool, when, when_contains

__all__ = ["FakeModel", "serve", "text", "tool", "when", "when_contains"]
