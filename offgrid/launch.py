"""Start OffGrid from a desktop launcher or the ``offgrid-launch`` command."""

from __future__ import annotations

import sys
from typing import List, Optional

from .cli import main as cli_main


def main(argv: Optional[List[str]] = None) -> int:
    """Start interactive chat, or pass an explicit CLI command through."""
    args = list(sys.argv[1:] if argv is None else argv)
    return cli_main(args or ["chat"])


if __name__ == "__main__":
    sys.exit(main())
