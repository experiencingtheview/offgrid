#!/usr/bin/env python3
"""Build a full offline OffGrid bundle for a USB stick.

This is a compatibility entry point. The implementation lives in
``offgrid.fullbundle`` so the distribution wizard can use it without shelling
out to a script.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from offgrid.fullbundle import main


if __name__ == "__main__":
    sys.exit(main())
