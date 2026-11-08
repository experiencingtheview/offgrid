#!/usr/bin/env python3
"""End-to-end demo with no model, no network and no downloads.

Runs the real orchestrator - planner, coder, tester, reviewer, lead - against a
scripted model server, in a throwaway directory, and prints the transcript.

    python3 scripts/demo.py

Use it to prove the install works, to see what a team run looks like, and as a
smoke test in CI. Exits non-zero if anything is off.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from offgrid.config import Config
from offgrid.orchestrator import Team
from offgrid.providers import LocalProvider
from offgrid.ui import UI
from offgrid.workspace import Workspace
from tests.fake_llm import serve, text, tool, when

PLAN = {
    "summary": "add a slugify() helper with tests",
    "steps": [
        {
            "id": 1,
            "title": "write slugify()",
            "agent": "coder",
            "task": "Create slugify.py exposing slugify(text) -> lowercase, hyphen separated",
            "files": ["slugify.py"],
            "depends_on": [],
        },
        {
            "id": 2,
            "title": "cover it with tests",
            "agent": "tester",
            "task": "Add test_slugify.py covering spaces, punctuation and leading/trailing junk",
            "files": ["test_slugify.py"],
            "depends_on": [1],
        },
    ],
    "verify": "python3 -m unittest discover -q",
}

SLUGIFY = '''import re


def slugify(text):
    """Turn arbitrary text into a url-safe slug."""
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")
'''

TESTS = '''import unittest

from slugify import slugify


class SlugifyTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_punctuation_and_spacing(self):
        self.assertEqual(slugify("  A  B!! "), "a-b")

    def test_empty(self):
        self.assertEqual(slugify("!!!"), "")


if __name__ == "__main__":
    unittest.main()
'''

APPROVE = '{"verdict": "approve", "issues": [], "instructions": ""}'
REPORT = (
    "Added slugify() in slugify.py and covered it with three tests.\n"
    "Files: slugify.py, test_slugify.py\n"
    "Verified: python3 -m unittest discover -q -> OK (3 tests)\n"
    "Nothing else needed."
)

RULES = [
    when("planner", text(json.dumps(PLAN))),
    when("coder", tool("write_file", path="slugify.py", content=SLUGIFY)),
    when("coder", tool("finish", summary="created slugify.py with slugify()")),
    when("tester", tool("write_file", path="test_slugify.py", content=TESTS)),
    when("tester", tool("run_command", command="python3 -m unittest discover -q")),
    when("tester", tool("finish", summary="3 tests pass")),
    when("reviewer", text(APPROVE)),
    when("lead", text(REPORT)),
]


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="offgrid-demo-"))
    model = serve(RULES)
    try:
        config = Config(
            endpoint=model.url,
            model="fake",
            command_policy="auto",
            color=sys.stdout.isatty(),
            verbose=2,
            workspace=str(workdir),
            # A scripted model has nothing to deliberate about, so the demo uses
            # the cheap profile. Real runs default to effort=best.
            effort="fast",
        )
        provider = LocalProvider(config)
        ui = UI(config)
        ui.banner(
            "add a slugify() helper with tests (demo mode, scripted model)",
            "scripted/fake",
            model.url,
        )
        team = Team(provider, Workspace(str(workdir)), config, ui)
        result = team.run("add a slugify() helper with tests")

        ui.rule()
        print()
        for line in result.summary.splitlines():
            print("  " + line)
        print()
        ui.ok(f"{len(result.changed)} file(s) changed: {', '.join(result.changed)}")
        if result.verify_ok:
            ui.ok(f"verified with `{result.verify_command}`")
        else:
            ui.error(f"verification failed: `{result.verify_command}`")
        ui.stats(result.stats.summary())
        print()
        ui.info(f"demo workspace: {workdir}")
        ui.info("this was a scripted model; swap in a real one with `offgrid doctor`")

        healthy = (
            (workdir / "slugify.py").is_file()
            and (workdir / "test_slugify.py").is_file()
            and result.verify_ok is True
            and result.review_verdict == "approve"
            and result.stats.model_calls == len(RULES)
        )
        return 0 if healthy else 1
    finally:
        model.stop()


if __name__ == "__main__":
    sys.exit(main())
