#!/usr/bin/env python3
"""Compatibility entry point for the built-in demo.

    python3 scripts/demo.py

The implementation lives in ``offgrid.demo`` so it also works after
``pip install``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from offgrid.demo import main


if __name__ == "__main__":
    sys.exit(main())
