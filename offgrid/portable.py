"""Build and install a portable OffGrid app.

The downloadable form is intentionally small: Python source, launch scripts and
a single-file ``.pyz``. It does not contain model weights or a Python runtime;
those belong in the full USB bundle made by ``scripts/setup_usb.py``.

Installing writes files into a *mounted* directory. It never opens a raw block
device, because writing an image there can destroy a disk and this project does
not make a bootable operating-system image.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipapp
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import __version__
from ._bundle import assets_root, missing_assets, package_root

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
  macOS (double-click)   OffGrid.command
  Windows (double-click) OffGrid.bat
  Linux                  ./OffGrid.sh
  Any terminal           python3 offgrid.pyz chat

The top-level launchers start interactive chat. The launch/ scripts are the
same entry points for terminals and scripts. The single-file app is offgrid.pyz;
the source tree is here if you want to read, test or change it. Both need an
existing Python 3.9+ and a local model server.

Put a shortcut on the desktop (Windows: OffGrid.bat desktop install):

  ./OffGrid.sh desktop install

A completely offline bundle, including a portable Python, llama.cpp and the
model file, is a separate and much larger build:

  python3 scripts/setup_usb.py --target /media/USB/offgrid --model 7b

Install or copy it to a mounted device from the app itself:

  python3 offgrid.pyz distribute --list
  python3 offgrid.pyz distribute --check-drive /media/USB --bundle dist/offgrid-portable.zip
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
FAT_FILESYSTEMS = {"fat", "fat32", "vfat", "msdos"}
DRIVE_MARGIN_GB = 1.0


class PortableError(Exception):
    """A bundle could not be built or installed safely."""


@dataclass(frozen=True)
class Device:
    path: Path
    label: str

    def as_dict(self) -> dict:
        return {"path": str(self.path), "label": self.label}


@dataclass(frozen=True)
class DriveReport:
    target: Path
    bundle: Path
    filesystem: str
    free_gb: float
    required_gb: float
    bundle_gb: float
    model_files: Tuple[str, ...]

    @property
    def model_checked(self) -> bool:
        return bool(self.model_files)

    def as_dict(self) -> dict:
        return {
            "target": str(self.target),
            "bundle": str(self.bundle),
            "filesystem": self.filesystem,
            "free_gb": self.free_gb,
            "required_gb": self.required_gb,
            "bundle_gb": self.bundle_gb,
            "model_files": list(self.model_files),
            "model_checked": self.model_checked,
        }


def source_root() -> Path:
    """Return the directory that contains the importable package.

    A wheel installs it under site-packages; a source checkout is its parent.
    Assets such as launchers and scripts are located separately because a
    wheel stores them under ``share/offgrid``.
    """
    root = package_root()
    if root is None:
        raise PortableError(
            "run the builder from an OffGrid source tree, a wheel install, or an unpacked bundle"
        )
    return root


def bundle_assets_root() -> Path:
    """Return the directory holding launchers, scripts, README and licence."""
    root = assets_root()
    if root is None:
        raise PortableError(
            "bundle assets are missing from this installation; reinstall OffGrid "
            "from a wheel or run it from a source checkout"
        )
    return root


def _require_assets(assets: Path, with_tests_from: Path, with_tests: bool) -> None:
    missing = missing_assets(assets)
    if with_tests and not (with_tests_from / "tests").is_dir():
        missing.append("tests/")
    if missing:
        raise PortableError(
            "this installation is missing bundle files: "
            + ", ".join(missing)
            + "; reinstall OffGrid from a wheel or use a source checkout"
        )


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


def _copy_required(staging: Path, source: Path, name: str) -> None:
    source_item = source / name
    if not source_item.exists():
        raise PortableError(f"bundle asset missing: {source_item}")
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
    assets = bundle_assets_root()
    _require_assets(assets, source, with_tests)
    destination = Path(destination).expanduser()
    if destination.exists() and not force:
        raise PortableError(f"{destination} already exists; pass --force to replace it")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    with tempfile.TemporaryDirectory(prefix="offgrid-bundle-") as tmp:
        staging = Path(tmp) / BUNDLE_PREFIX
        staging.mkdir()
        _copy_required(staging, source, "offgrid")
        for name in (
            "launch",
            "scripts",
            "OffGrid.sh",
            "OffGrid.command",
            "OffGrid.bat",
            "pyproject.toml",
            "README.md",
            "LICENSE",
        ):
            _copy_required(staging, assets, name)
        if with_tests:
            _copy_required(staging, source, "tests")
        for launcher in (
            "launch/offgrid.sh",
            "launch/offgrid.command",
            "OffGrid.sh",
            "OffGrid.command",
        ):
            candidate = staging / launcher
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
    source = Path(source) if source is not None else source_root()
    assets = bundle_assets_root()
    _require_assets(assets, source, with_tests)
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


def is_mount_point(path: Path) -> bool:
    """Whether *path* is a mounted filesystem root, such as a USB drive."""
    try:
        return os.path.ismount(Path(path).expanduser().resolve())
    except OSError:
        return False


def _run_filesystem_command(args: Sequence[str]) -> str:
    try:
        proc = subprocess.run(
            list(args), capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _windows_filesystem(path: Path) -> str:
    try:
        import ctypes

        drive = os.path.splitdrive(str(Path(path).resolve()))[0]
        root = f"{drive}\\" if drive else "C:\\"
        name = ctypes.create_unicode_buffer(256)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), None, 0, None, None, None, name, len(name)
        )
        return name.value if ok else ""
    except Exception:
        return ""


def filesystem_type(path: Path) -> str:
    """Best-effort filesystem name without adding a dependency."""
    path = Path(path).expanduser()
    if os.name == "nt":
        return _windows_filesystem(path) or "unknown"
    if sys.platform == "darwin":
        command = ("stat", "-f", "%HT", str(path))
    else:
        command = ("stat", "-f", "-c", "%T", str(path))
    found = _run_filesystem_command(command)
    if found:
        return found
    # Linux fallback for stat implementations that do not support -f -c.
    if sys.platform != "darwin":
        output = _run_filesystem_command(("df", "-P", "-T", str(path)))
        lines = [line for line in output.splitlines() if line.strip()]
        if len(lines) > 1 and len(lines[1].split()) > 1:
            return lines[1].split()[1]
    return "unknown"


def _hash_stream(handle) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _known_model_hashes() -> Dict[str, str]:
    """Pinned hashes for the GGUF files shipped by the full-bundle builder."""
    from . import fullbundle

    return {
        str(spec["file"]): str(spec["sha256"])
        for spec in fullbundle.MODELS.values()
        if spec.get("file") and spec.get("sha256")
    }


def _verify_model_stream(name: str, handle) -> None:
    expected = _known_model_hashes().get(Path(name).name)
    if not expected:
        raise PortableError(
            f"cannot verify {Path(name).name}: no pinned SHA-256 for that model; "
            "use a bundled 3b/7b/14b/35b/80b model or remove it"
        )
    actual = _hash_stream(handle)
    if actual.lower() != expected.lower():
        raise PortableError(
            f"SHA-256 mismatch for {Path(name).name}: expected {expected}, got {actual}; "
            "do not transfer this bundle"
        )


def _verify_bundle_models(bundle: Path) -> Tuple[str, ...]:
    if bundle.is_dir():
        paths = sorted(path for path in bundle.rglob("*.gguf") if path.is_file())
        for path in paths:
            try:
                with path.open("rb") as handle:
                    _verify_model_stream(path.name, handle)
            except OSError as exc:
                raise PortableError(f"could not read model {path}: {exc}") from exc
        return tuple(path.name for path in paths)

    if bundle.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(bundle) as archive:
                infos = [
                    info
                    for info in archive.infolist()
                    if info.filename.lower().endswith(".gguf") and not info.is_dir()
                ]
                for info in infos:
                    with archive.open(info) as handle:
                        _verify_model_stream(info.filename, handle)
                return tuple(Path(info.filename).name for info in infos)
        except (OSError, zipfile.BadZipFile) as exc:
            raise PortableError(f"could not read bundle {bundle}: {exc}") from exc
    return ()


def bundle_size_bytes(bundle: Path) -> int:
    if bundle.is_dir():
        try:
            return sum(path.stat().st_size for path in bundle.rglob("*") if path.is_file())
        except OSError as exc:
            raise PortableError(f"could not measure {bundle}: {exc}") from exc
    if bundle.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(bundle) as archive:
                return sum(info.file_size for info in archive.infolist() if not info.is_dir())
        except (OSError, zipfile.BadZipFile) as exc:
            raise PortableError(f"could not measure {bundle}: {exc}") from exc
    try:
        return bundle.stat().st_size
    except OSError as exc:
        raise PortableError(f"could not measure {bundle}: {exc}") from exc


def check_usb_drive(
    target: Path,
    bundle: Path,
    *,
    require_exfat: bool = True,
    margin_gb: float = DRIVE_MARGIN_GB,
) -> DriveReport:
    """Check a mounted target and bundle before copying anything."""
    target = _validate_target(target)
    bundle = Path(bundle).expanduser().resolve()
    if not bundle.exists():
        raise PortableError(f"bundle not found: {bundle}")
    if not os.access(target, os.W_OK):
        raise PortableError(f"target is not writable: {target}")

    filesystem = filesystem_type(target)
    name = filesystem.lower()
    if name in FAT_FILESYSTEMS:
        raise PortableError(
            f"{target} is {filesystem}, which cannot hold a 4 GB+ GGUF; "
            "format it as exFAT"
        )
    if require_exfat and name != "exfat":
        raise PortableError(
            f"{target} is {filesystem or 'unknown'}, not exFAT; "
            "format the drive as exFAT or choose another target"
        )

    size = bundle_size_bytes(bundle)
    bundle_gb = round(size / (1024 ** 3), 1)
    required_gb = round(bundle_gb + max(0.0, margin_gb), 1)
    try:
        free_gb = round(shutil.disk_usage(target).free / (1024 ** 3), 1)
    except OSError as exc:
        raise PortableError(f"could not measure free space on {target}: {exc}") from exc
    if free_gb < required_gb:
        raise PortableError(
            f"not enough free space on {target}: need about {required_gb:.1f} GB, "
            f"have {free_gb:.1f} GB"
        )

    model_files = _verify_bundle_models(bundle)
    return DriveReport(
        target=target,
        bundle=bundle,
        filesystem=filesystem,
        free_gb=free_gb,
        required_gb=required_gb,
        bundle_gb=bundle_gb,
        model_files=model_files,
    )


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
        if relative.name.lower() in ("offgrid.sh", "offgrid.command") or relative.suffix.lower() == ".pyz":
            permissions |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        if permissions:
            try:
                destination.chmod(permissions)
            except OSError:
                pass


def _validate_bundle_directory(bundle: Path) -> Path:
    """Check the shape and links of a full-bundle directory before copying.

    Runtime archives can contain harmless internal file links (for example a
    portable Python's ``bin/python3``). Those are copied as their target file,
    because a USB filesystem may not support symlinks. Links that escape the
    bundle, point at directories, or are broken are refused.
    """
    bundle = Path(bundle).expanduser().resolve()
    if not bundle.is_dir():
        raise PortableError(f"bundle directory not found: {bundle}")
    if not (bundle / "launch").is_dir():
        raise PortableError(f"full-bundle directory must contain launch/: {bundle}")
    if not (bundle / "offgrid").is_dir() and not (bundle / "offgrid.pyz").is_file():
        raise PortableError(
            f"full-bundle directory must contain offgrid/ or offgrid.pyz: {bundle}"
        )
    try:
        for path in bundle.rglob("*"):
            if not path.is_symlink():
                continue
            label = path.relative_to(bundle)
            try:
                target = path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise PortableError(f"broken symlink in bundle: {label}") from exc
            try:
                target.relative_to(bundle)
            except ValueError:
                raise PortableError(
                    f"symlink in bundle points outside the bundle: {label}"
                ) from None
            if target.is_dir():
                raise PortableError(
                    f"symlinked directory in bundle: {label}; copy its contents into the bundle"
                )
    except OSError as exc:
        raise PortableError(f"could not read bundle directory {bundle}: {exc}") from exc
    return bundle


def _bundle_destination(bundle: Path, target: Path) -> Path:
    """Choose the installed folder and refuse a copy that would recurse."""
    if not bundle.name or bundle == bundle.parent:
        raise PortableError("choose a bundle directory, not a filesystem root")
    destination = target / bundle.name
    try:
        destination.resolve().relative_to(target)
    except ValueError:
        raise PortableError(f"bundle destination escapes target: {destination}") from None
    try:
        destination.resolve().relative_to(bundle)
    except ValueError:
        return destination
    raise PortableError(
        "bundle destination is inside the source bundle; choose another target"
    )


def _install_bundle_directory(
    bundle: Path,
    target: Path,
    force: bool = False,
    dry_run: bool = False,
) -> Path:
    """Copy a full-bundle directory into the mounted target."""
    bundle = _validate_bundle_directory(bundle)
    destination = _bundle_destination(bundle, target)
    if destination.exists():
        if not force:
            raise PortableError(f"{destination} already exists; pass --force to replace it")
        if not destination.is_dir():
            raise PortableError(
                f"{destination} exists and is not a directory; remove it first"
            )
    if not dry_run:
        shutil.copytree(
            bundle,
            destination,
            dirs_exist_ok=force,
            copy_function=shutil.copy2,
        )
        for name in (
            "launch/offgrid.sh",
            "launch/offgrid.command",
            "OffGrid.sh",
            "OffGrid.command",
            "offgrid.pyz",
        ):
            candidate = destination / name
            if candidate.is_file():
                _make_executable(candidate)
    return destination


def install_bundle(
    bundle: Path,
    target: Path,
    force: bool = False,
    dry_run: bool = False,
) -> Path:
    """Copy a ``.pyz``, unpack a portable zip, or install a full-bundle directory."""
    bundle = Path(bundle).expanduser().resolve()
    target = _validate_target(target)
    if not bundle.exists():
        raise PortableError(f"bundle not found: {bundle}")
    if bundle.is_dir():
        return _install_bundle_directory(bundle, target, force=force, dry_run=dry_run)
    if not bundle.is_file():
        raise PortableError("bundle must be a .zip, .pyz, or full-bundle directory")

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
