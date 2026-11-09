#!/usr/bin/env python3
"""Check that a release tag matches the version the package reports.

The version lives in exactly one place, ``offgrid/__init__.py``. The release
workflow runs this before it builds anything, so a tag that disagrees with that
file fails instead of publishing a bundle labelled with another version.

Usage:
    python3 scripts/check_release_version.py v0.1.0
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "offgrid" / "__init__.py"


def package_version() -> str:
    """The single source of truth, read from the file rather than imported."""
    text = INIT.read_text(encoding="utf-8")
    for line in text.splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == "__version__":
            return value.strip().strip('"').strip("'")
    raise SystemExit(f"could not find __version__ in {INIT}")


def tag_version(tag: str) -> str:
    """Accept v0.1.0 or 0.1.0 and return 0.1.0."""
    return tag[1:] if tag.startswith("v") else tag


def problem(tag: str, version: str) -> str:
    """Why this tag cannot be published, or an empty string when it can."""
    if tag_version(tag) != version:
        return (
            f"tag {tag!r} does not match offgrid {version}: "
            f"bump __version__ in offgrid/__init__.py, or tag v{version}"
        )
    return ""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="the release tag, for example v0.1.0")
    args = parser.parse_args(argv)
    version = package_version()
    message = problem(args.tag, version)
    if message:
        print(message, file=sys.stderr)
        return 1
    print(f"tag {args.tag} matches offgrid {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
