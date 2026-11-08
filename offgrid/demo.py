"""Run the real agent team against a scripted model, with no downloads.

The demo is shipped in the package so ``offgrid demo`` works from an installed
wheel as well as from a source checkout. It uses a throwaway workspace and
localhost-only fake server; no model weights, account or internet are needed.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .config import Config
from .fake_llm import serve, text, tool, when
from .orchestrator import Team
from .providers import LocalProvider
from .ui import UI
from .workspace import Workspace

if os.name == "nt":
    VERIFY = subprocess.list2cmdline([sys.executable, "-m", "unittest", "discover", "-q"])
else:
    VERIFY = f"{shlex.quote(sys.executable)} -m unittest discover -q"
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
    "verify": VERIFY,
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
    f"Verified: {VERIFY} -> OK (3 tests)\n"
    "Nothing else needed."
)

RULES = [
    when("planner", text(json.dumps(PLAN))),
    when("coder", tool("write_file", path="slugify.py", content=SLUGIFY)),
    when("coder", tool("finish", summary="created slugify.py with slugify()")),
    when("tester", tool("write_file", path="test_slugify.py", content=TESTS)),
    when("tester", tool("run_command", command=VERIFY)),
    when("tester", tool("finish", summary="3 tests pass")),
    when("reviewer", text(APPROVE)),
    when("lead", text(REPORT)),
]


def run_demo(color: Optional[bool] = None) -> int:
    """Run the scripted demo and return 0 only when the whole team succeeded."""
    if color is None:
        color = sys.stdout.isatty()
    workdir = Path(tempfile.mkdtemp(prefix="offgrid-demo-"))
    model = serve(RULES)
    try:
        config = Config(
            endpoint=model.url,
            model="fake",
            command_policy="auto",
            color=color,
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
        for line in (result.summary or "").splitlines():
            print("  " + line)
        print()
        ui.ok(f"{len(result.changed)} file(s) changed: {', '.join(result.changed)}")
        if result.verify_ok:
            ui.ok(f"verified with `{result.verify_command}`")
        else:
            ui.error(f"verification failed: `{result.verify_command}`")
        if result.stats:
            ui.stats(result.stats.summary())
        print()
        ui.info(f"demo workspace: {workdir}")
        ui.info("this was a scripted model; swap in a real one with `offgrid doctor`")

        healthy = (
            (workdir / "slugify.py").is_file()
            and (workdir / "test_slugify.py").is_file()
            and result.verify_ok is True
            and result.review_verdict == "approve"
            and result.stats is not None
            and result.stats.model_calls == len(RULES)
        )
        return 0 if healthy else 1
    finally:
        model.stop()


def main() -> int:
    return run_demo()


if __name__ == "__main__":
    sys.exit(main())
