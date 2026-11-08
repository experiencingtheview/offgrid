"""The honesty contract: prompts, tools and defaults must all support admitting ignorance."""

import tempfile
import unittest
from pathlib import Path

from offgrid.agents import ROSTER, get_agent
from offgrid.config import Config
from offgrid.council import Council
from offgrid.memory import MemoryStore
from offgrid.tools import ToolBox
from offgrid.workspace import Workspace


class PromptTests(unittest.TestCase):
    def test_every_agent_gets_the_epistemic_rules(self):
        for key in ROSTER:
            system = get_agent(key).system()
            self.assertIn("EPISTEMIC RULES", system, key)
            self.assertIn("I don't know", system, key)
            self.assertIn("Never invent", system, key)
            self.assertIn("not verified", system, key)

    def test_rules_come_last_so_they_are_weighted(self):
        system = get_agent("coder").system()
        self.assertTrue(system.rstrip().endswith("was not verified."))

    def test_archivist_exists_and_asks_for_empty_lessons(self):
        archivist = get_agent("archivist")
        self.assertIn("lessons", archivist.system())
        self.assertIn("nothing durable", archivist.system())

    def test_analyst_prefers_repo_then_library_then_council(self):
        system = get_agent("analyst").system().lower()
        self.assertLess(system.index("repository"), system.index("offline library"))
        self.assertLess(system.index("offline library"), system.index("council"))

    def test_council_advisor_prompt_refuses_invention(self):
        from offgrid.council import ADVISOR_SYSTEM

        self.assertIn("I don't know", ADVISOR_SYSTEM)
        self.assertIn("Do not invent", ADVISOR_SYSTEM)


class DefaultsTests(unittest.TestCase):
    def test_council_is_off_by_default(self):
        self.assertFalse(Config().council)
        self.assertFalse(Config().allow_public_council)

    def test_memory_is_on_by_default(self):
        self.assertTrue(Config().memory)


class ToolHonestyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = Config(command_policy="auto", color=False, verbose=0)

    def tearDown(self):
        self.tmp.cleanup()

    def toolbox(self, **kwargs):
        return ToolBox(Workspace(str(self.root)), self.config, **kwargs)

    def test_ask_council_without_configuration_admits_ignorance(self):
        tools = self.toolbox(council=Council(self.config))
        result = tools.ask_council("what does this unknown flag do")
        self.assertFalse(result.ok)
        self.assertIn("council unavailable", result.content)
        self.assertIn("do not know", result.content.lower())

    def test_recall_without_memory_is_explicit(self):
        result = self.toolbox().recall("anything")
        self.assertFalse(result.ok)
        self.assertIn("memory is disabled", result.content)

    def test_library_search_when_empty_says_so(self):
        from offgrid.library import Library

        tools = self.toolbox(library=Library(self.root / "lib"))
        result = tools.library_search("water purification")
        self.assertTrue(result.ok)
        self.assertIn("library is empty", result.content)
        self.assertIn("rather than guessing", result.content)

    def test_library_read_refuses_non_text(self):
        from offgrid.library import Library

        lib_root = self.root / "lib"
        (lib_root / "coding").mkdir(parents=True)
        (lib_root / "coding" / "docs.zim").write_text("x")
        tools = self.toolbox(library=Library(lib_root))
        result = tools.library_read("coding/docs.zim")
        self.assertFalse(result.ok)
        self.assertIn("ZIM", result.content)

    def test_remember_and_recall_roundtrip(self):
        memory = MemoryStore(self.root / "m" / "memory.db")
        self.addCleanup(memory.close)
        tools = self.toolbox(memory=memory)
        self.assertTrue(tools.remember("the verify command is make test", kind="repo").ok)
        found = tools.recall("verify command")
        self.assertTrue(found.ok)
        self.assertIn("make test", found.content)


if __name__ == "__main__":
    unittest.main()
