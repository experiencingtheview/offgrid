#!/usr/bin/env python3
"""Print the CHANGELOG.md section for a release tag, as release notes.

The section heading looks like ``## [0.1.0] - 2026-11-08``. The body under it is
printed verbatim, so the notes on the release page are the text the repository
records for that version instead of a second copy that drifts.

Usage:
    python3 scripts/changelog_notes.py v0.1.0 > dist/notes.md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
HEADING = re.compile(r"^##\s+\[?(?P<version>[^\s\]]+)\]?(?:\s+-\s+.*)?$")


def notes_for(text: str, tag: str) -> str:
    """The body of the changelog section for ``tag``, without its heading."""
    wanted = tag[1:] if tag.startswith("v") else tag
    body: List[str] = []
    seen = False
    inside = False
    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            inside = heading.group("version") == wanted
            seen = seen or inside
            continue
        if inside:
            body.append(line)
    if not seen:
        raise LookupError(
            f"CHANGELOG.md has no section for {wanted}: "
            f"add a '## [{wanted}] - <date>' section before tagging"
        )
    notes = "\n".join(body).strip()
    if not notes:
        raise LookupError(f"CHANGELOG.md has an empty section for {wanted}")
    return notes


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="the release tag, for example v0.1.0")
    args = parser.parse_args(argv)
    try:
        notes = notes_for(CHANGELOG.read_text(encoding="utf-8"), args.tag)
    except LookupError as error:
        print(error, file=sys.stderr)
        return 1
    print(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
