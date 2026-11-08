"""Build and install a portable OffGrid app.

The downloadable form is intentionally small: Python source, launch scripts and
a single-file ``.pyz``. It does not contain model weights or a Python runtime;
those belong in the full USB bundle made by ``scripts/setup_usb.py``.

Installing writes files into a *mounted* directory. It never opens a raw block
device, because writing an image there can destroy a disk and this project does
not make a bootable operating-system image.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import zipapp
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Optional, Sequence, Tuple

from . import __version__

BUNDLE_PREFIX = f"offgrid-{__version__}"
PYZ_NAME = "offgrid.pyz"
DEFAULT_BUNDLE = Path("dist") / f"{BUNDLE_PREFIX}-portable.zip"
README_FIRST = f"""\
OffGrid portable bundle
=======================

This folder can be copied to a USB stick or another computer. It needs Python
3.9 or newer and a local OpenAI-compatible model server, such as Ollama,
llama.cpp, LM Studio or vLLM. It does not include model weights or a Python
runtime.

Quick start
-----------
  python3 offgrid.pyz doctor
  python3 offgrid.pyz chat
  python3 offgrid.pyz run "fix the failing test"

The single-file app is offgrid.pyz. The source tree is also here if you want to
read, test or change it. The launch/ scripts work with an existing Python and
the source tree.

A completely offline bundle, including a portable Python, llama.cpp and the
model file, is a separate and much larger build:

  python3 scripts/setup_usb.py --target /media/USB/offgrid --model 7b

Install or copy it to a mounted device from the app itself:

  python3 offgrid.pyz distribute --list
  python3 offgrid.pyz distribute --to /media/USB

OffGrid never writes to a raw block device. Install to the mounted filesystem,
then unmount it normally.
"""

SYSTEM_TARGETS = {
    "/",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib64",
    "/proc",
    "/sbin",
    "/sys",
    "/usr",
    "/var",
}
WINDOWS_SYSTEM_TARGETS = {
    "c:",
    "c:/windows",
    "c:/program files",
    "c:/program files (x86)",
}


class PortableError(Exception):
    """A bundle could not be built or installed safely."""


@dataclass(frozen=True)
class Device:
    path: Path
    label: str

    def as_dict(self) -> dict:
        return {"path": str(self.path), "label": self.label}


def source_root() -> Path:
    root = Path(__file__).resolve().parent.parent
    if not (root / "offgrid" / "cli.py").is_file():
        raise PortableError("run the builder from an OffGrid source tree or an unpacked bundle")
    return root


def _make_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _ignore(directory: str, names: List[str]) -> Iterable[str]:
    ignored = {"__pycache__", ".offgrid", ".pytest_cache", "*.egg-info"}
    return [name for name in names if name in ignored or name.endswith(".pyc")]


def build_pyz(destination: Path, source: Optional[Path] = None, force: bool = False) -> Path:
    """Build the single-file application with the standard-library zipapp."""
    source = Path(source) if source is not None else source_root()
    destination = Path(destination).expanduser()
    if destination.exists() and not force:
        raise PortableError(f"{destination} already exists; pass --force to replace it")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    with tempfile.TemporaryDirectory(prefix="offgrid-pyz-") as tmp:
        staging = Path(tmp) / "app"
        shutil.copytree(source / "offgrid", staging / "offgrid", ignore=_ignore)
        (staging / "__main__.py").write_text(
            "import sys\n\nfrom offgrid.cli import main\n\n"
            "if __name__ == \"__main__\":\n    sys.exit(main())\n",
            encoding="utf-8",
        )
        zipapp.create_archive(
            str(staging),
            target=str(destination),
            interpreter="/usr/bin/env python3",
            compressed=True,
        )
    _make_executable(destination)
    return destination


def _copy_item(staging: Path, source: Path, name: str) -> None:
    source_item = source / name
    if not source_item.exists():
        return
    destination = staging / name
    if source_item.is_dir():
        shutil.copytree(source_item, destination, ignore=_ignore)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_item, destination)


def _write_bundle_zip(staging: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(staging.rglob("*")):
            if not path.is_file():
                continue
            archive.write(path, path.relative_to(staging.parent).as_posix())


def build_bundle(
    destination: Path,
    source: Optional[Path] = None,
    with_tests: bool = False,
    force: bool = False,
    pyz_path: Optional[Path] = None,
) -> Path:
    """Build a zip containing source, launchers and the single-file app."""
    source = Path(source) if source is not None else source_root()
    destination = Path(destination).expanduser()
    if destination.exists() and not force:
        raise PortableError(f"{destination} already exists; pass --force to replace it")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    with tempfile.TemporaryDirectory(prefix="offgrid-bundle-") as tmp:
        staging = Path(tmp) / BUNDLE_PREFIX
        staging.mkdir()
        for name in ("offgrid", "launch", "scripts", "pyproject.toml", "README.md", "LICENSE"):
            _copy_item(staging, source, name)
        if with_tests:
            _copy_item(staging, source, "tests")
        for launcher in ("offgrid.sh", "offgrid.command"):
            candidate = staging / "launch" / launcher
            if candidate.is_file():
                _make_executable(candidate)
        if pyz_path and Path(pyz_path).is_file():
            shutil.copy2(pyz_path, staging / PYZ_NAME)
        else:
            build_pyz(staging / PYZ_NAME, source=source)
        (staging / "README-FIRST.txt").write_text(README_FIRST, encoding="utf-8")
        _write_bundle_zip(staging, destination)
    return destination


def build_distribution(
    destination: Path,
    source: Optional[Path] = None,
    with_tests: bool = False,
    force: bool = False,
) -> Tuple[Path, Path]:
    """Build both the portable zip and its standalone ``.pyz`` sibling."""
    destination = Path(destination).expanduser()
    if destination.suffix.lower() != ".zip":
        destination = destination.with_suffix(".zip")
    pyz_path = destination.with_suffix(".pyz")
    if not force:
        for path in (destination, pyz_path):
            if path.exists():
                raise PortableError(f"{path} already exists; pass --force to replace it")
    build_pyz(pyz_path, source=source, force=force)
    build_bundle(destination, source=source, with_tests=with_tests, force=force, pyz_path=pyz_path)
    return destination, pyz_path


# ---------------------------------------------------------------------------
# connected, mounted devices
# ---------------------------------------------------------------------------
def _mount_roots() -> List[Path]:
    if os.name == "nt":
        if hasattr(os, "listdrives"):
            return [Path(drive) for drive in os.listdrives()]
        return [Path(f"{letter}:\\") for letter in "DEFGHIJKLMNOPQRSTUVWXYZ"]
    if sys.platform == "darwin":
        return [Path("/Volumes")]
    user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    roots = [
        Path("/media") / user if user else Path("/media"),
        Path("/run/media") / user if user else Path("/run/media"),
        Path("/media"),
        Path("/mnt"),
    ]
    unique: List[Path] = []
    seen = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def connected_devices(roots: Optional[Sequence[Path]] = None) -> List[Device]:
    """List mounted child directories that look like removable targets.

    This is a convenience list, not a promise that every path is removable.
    The caller still chooses the target explicitly.
    """
    devices: List[Device] = []
    seen = set()
    for root in roots if roots is not None else _mount_roots():
        root = Path(root)
        try:
            if not root.is_dir():
                continue
            children = sorted(root.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            continue
        for child in children:
            try:
                if not child.is_dir() or child.name.startswith("."):
                    continue
                resolved = child.resolve()
            except OSError:
                continue
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            devices.append(Device(path=resolved, label=child.name))
    return devices


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------
def _validate_target(target: Path) -> Path:
    resolved = Path(target).expanduser().resolve()
    if not resolved.exists():
        raise PortableError(f"target does not exist: {resolved}")
    if not resolved.is_dir():
        raise PortableError(f"target is not a mounted directory: {resolved}")
    text = str(resolved).replace("\\", "/").lower().rstrip("/")
    if resolved == Path("/") or text in SYSTEM_TARGETS or text in WINDOWS_SYSTEM_TARGETS:
        raise PortableError(f"refusing a system location: {resolved}")
    if resolved == Path.home().resolve():
        raise PortableError("refusing to install into your home directory; choose the device root")
    return resolved


def _safe_extract(archive: zipfile.ZipFile, target: Path) -> None:
    base = target.resolve()
    for info in archive.infolist():
        relative = PurePosixPath(info.filename)
        if relative.is_absolute() or ".." in relative.parts:
            raise PortableError(f"unsafe path in bundle: {info.filename}")
        if relative.parts and relative.parts[0].endswith(":"):
            raise PortableError(f"unsafe drive path in bundle: {info.filename}")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise PortableError(f"symlink in bundle: {info.filename}")
        destination = (base / Path(*relative.parts)).resolve()
        try:
            destination.relative_to(base)
        except ValueError:
            raise PortableError(f"path escapes target: {info.filename}") from None
        if info.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, destination.open("wb") as output:
            shutil.copyfileobj(source, output)
        permissions = mode & 0o777
        if relative.name in ("offgrid.sh", "offgrid.command") or relative.suffix.lower() == ".pyz":
            permissions |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        if permissions:
            try:
                destination.chmod(permissions)
            except OSError:
                pass


def install_bundle(
    bundle: Path,
    target: Path,
    force: bool = False,
    dry_run: bool = False,
) -> Path:
    """Copy a ``.pyz`` or unpack a portable zip into a mounted directory."""
    bundle = Path(bundle).expanduser().resolve()
    target = _validate_target(target)
    if not bundle.exists() or not bundle.is_file():
        raise PortableError(f"bundle not found: {bundle}")

    suffix = bundle.suffix.lower()
    if suffix == ".pyz":
        destination = target / bundle.name
        if destination.exists() and not force:
            raise PortableError(f"{destination} already exists; pass --force to replace it")
        if not dry_run:
            shutil.copy2(bundle, destination)
            try:
                destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
            except OSError:
                pass
        return destination

    if suffix != ".zip":
        raise PortableError("bundle must be a .zip or .pyz file")

    with zipfile.ZipFile(bundle) as archive:
        roots = {
            PurePosixPath(info.filename).parts[0]
            for info in archive.infolist()
            if PurePosixPath(info.filename).parts
        }
        if len(roots) != 1:
            raise PortableError("bundle must contain one top-level folder")
        root_name = next(iter(roots))
        destination = target / root_name
        if destination.exists() and not force:
            raise PortableError(f"{destination} already exists; pass --force to replace it")
        if not dry_run:
            _safe_extract(archive, target)
    return destination


def find_default_bundle() -> Optional[Path]:
    for candidate in (DEFAULT_BUNDLE, DEFAULT_BUNDLE.with_suffix(".pyz")):
        if candidate.is_file():
            return candidate
    if sys.argv:
        self_path = Path(sys.argv[0]).expanduser()
        if self_path.is_file() and self_path.suffix.lower() == ".pyz":
            return self_path
    return None
