"""Locate the source tree and the files that make a distributable bundle.

An installed wheel contains the ``offgrid`` package under ``site-packages`` but
not the launchers, setup scripts, README or licence. Those are installed as
data files under ``share/offgrid``. Keeping the lookup in one place means the
portable zip and the full USB bundle fail clearly when an installation is
incomplete, instead of silently producing a bundle that cannot launch.
"""

from __future__ import annotations

import sysconfig
from pathlib import Path
from typing import Optional

PACKAGE_PARENT = Path(__file__).resolve().parent.parent
MIN_ASSETS = (
    "OffGrid.sh",
    "OffGrid.command",
    "OffGrid.bat",
    "launch/offgrid.sh",
    "launch/offgrid.command",
    "launch/offgrid.bat",
    "scripts/setup_usb.py",
    "README.md",
    "LICENSE",
    "pyproject.toml",
)


def package_root() -> Optional[Path]:
    """Return the directory containing the importable ``offgrid`` package."""
    if (PACKAGE_PARENT / "offgrid" / "cli.py").is_file():
        return PACKAGE_PARENT
    return None


def source_root() -> Optional[Path]:
    """Return a complete source checkout, if this is one."""
    root = package_root()
    if root is None:
        return None
    if all((root / name).is_file() for name in MIN_ASSETS):
        return root
    return None


def installed_assets_root() -> Optional[Path]:
    """Return the wheel's ``share/offgrid`` data directory, if complete."""
    try:
        data = sysconfig.get_path("data")
    except (KeyError, OSError):
        return None
    if not data:
        return None
    root = Path(data) / "share" / "offgrid"
    if all((root / name).is_file() for name in MIN_ASSETS):
        return root
    return None


def assets_root() -> Optional[Path]:
    """Find launchers, setup scripts and docs in a checkout or an install."""
    return source_root() or installed_assets_root()


def missing_assets(root: Optional[Path]) -> list[str]:
    """Names of required bundle assets absent from ``root``."""
    if root is None:
        return list(MIN_ASSETS)
    return [name for name in MIN_ASSETS if not (root / name).is_file()]
