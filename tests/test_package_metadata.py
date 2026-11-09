"""Packaging checks for the built wheel.

The normal test suite does not require a build frontend. The CI package job
sets OFFGRID_WHEEL to the wheel it built so these checks run against the
actual artifact rather than source metadata.
"""

from __future__ import annotations

import os
import unittest
import zipfile
from email.parser import Parser
from pathlib import Path

HOMEPAGE = "https://site-936361bbc8d4492da54f8727d27c6b06.freebuff.page"
HOMEPAGE_LINE = (
    "**Project homepage:** "
    f"[site-936361bbc8d4492da54f8727d27c6b06.freebuff.page]({HOMEPAGE})"
)


class WheelMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        wheel = os.environ.get("OFFGRID_WHEEL")
        if not wheel:
            raise unittest.SkipTest("set OFFGRID_WHEEL to a built wheel")
        cls.wheel = Path(wheel)
        if not cls.wheel.is_file():
            raise FileNotFoundError(f"wheel does not exist: {cls.wheel}")
        with zipfile.ZipFile(cls.wheel) as archive:
            metadata_names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise AssertionError(
                    f"expected one METADATA file, found {metadata_names}"
                )
            cls.metadata = Parser().parsestr(
                archive.read(metadata_names[0]).decode("utf-8")
            )

    def test_homepage_project_url(self) -> None:
        project_urls = self.metadata.get_all("Project-URL") or []
        self.assertIn(f"Homepage, {HOMEPAGE}", project_urls)

    def test_long_description_contains_homepage(self) -> None:
        description = self.metadata.get_payload()
        self.assertIsInstance(description, str)
        self.assertIn("# OffGrid", description)
        self.assertIn(HOMEPAGE_LINE, description)
        self.assertIn(
            "text/markdown",
            (self.metadata.get("Description-Content-Type") or "").lower(),
        )
        self.assertGreater(len(description), 1000)
