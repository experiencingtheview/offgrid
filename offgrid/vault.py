"""Encrypting data at rest, and the honest limits of "encrypt everything".

This tool encrypts a file or a folder into a single `.ogv` archive. It uses the
system `openssl` for AES-256-CBC with a fresh PBKDF2 salt, and adds an
encrypt-then-MAC HMAC-SHA256 (key derived separately with scrypt) so a wrong
passphrase or a modified file is detected before any plaintext is written.

It deliberately does **not** encrypt the running operating system. Full-disk
encryption is performed by the OS before boot (BitLocker, FileVault, LUKS); doing
it from inside a running session risks destroying the machine with no way back.
`plan()` says so, and gives the real route for each platform.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Iterable, List, Optional

MAGIC = b"OFFGRID1"
TAG_LEN = 32
SALT_LEN = 8
ITERATIONS = 200_000
MAX_INPUT_BYTES = 512 * 1024 * 1024
MIN_PASSPHRASE = 8


class VaultError(Exception):
    """Encryption refused or failed, with a reason a person can act on."""


def backend() -> str:
    return "openssl" if shutil.which("openssl") else ""


def available() -> bool:
    return bool(backend())


def _openssl(data: bytes, passphrase: str, decrypt: bool) -> bytes:
    binary = shutil.which("openssl")
    if not binary:
        raise VaultError(
            "openssl is not installed. OffGrid will not hand-roll its own cipher: install "
            "OpenSSL (or use your OS full-disk encryption) instead."
        )
    if not passphrase:
        raise VaultError("a passphrase is required")
    if len(data) > MAX_INPUT_BYTES:
        raise VaultError(
            f"input is larger than {MAX_INPUT_BYTES // (1024 * 1024)} MB; split it before encrypting"
        )
    with tempfile.TemporaryDirectory(prefix="offgrid-vault-") as tmp:
        source = Path(tmp) / "input"
        target = Path(tmp) / "output"
        source.write_bytes(data)
        command = [
            binary,
            "enc",
            "-aes-256-cbc",
            "-salt",
            "-pbkdf2",
            "-iter",
            str(ITERATIONS),
            "-md",
            "sha256",
            "-pass",
            "stdin",
            "-in",
            str(source),
            "-out",
            str(target),
        ]
        if decrypt:
            command.insert(2, "-d")
        completed = subprocess.run(
            command,
            input=passphrase + "\n",
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()
            reason = detail[-1] if detail else "openssl refused the input"
            raise VaultError(f"openssl failed: {reason}")
        return target.read_bytes()


def _mac_key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt + b"|offgrid-mac",
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )


def seal(data: bytes, passphrase: str) -> bytes:
    """Encrypt and authenticate `data`. Wrong passphrase or tampering is detectable."""
    body = _openssl(data, passphrase, decrypt=False)
    if not body.startswith(b"Salted__") or len(body) < 16:
        raise VaultError("openssl did not return a salted payload; refusing to store it")
    salt = body[SALT_LEN : SALT_LEN + SALT_LEN]
    mac = hmac.new(_mac_key(passphrase, salt), MAGIC + body, hashlib.sha256).digest()
    return MAGIC + body + mac


def unseal(blob: bytes, passphrase: str) -> bytes:
    if not blob.startswith(MAGIC):
        raise VaultError("not an OffGrid vault file (bad header)")
    body = blob[len(MAGIC) : -TAG_LEN]
    tag = blob[-TAG_LEN:]
    if not body.startswith(b"Salted__") or len(body) < 16:
        raise VaultError("vault file is truncated")
    salt = body[SALT_LEN : SALT_LEN + SALT_LEN]
    expected = hmac.new(_mac_key(passphrase, salt), MAGIC + body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise VaultError("wrong passphrase, or the file was modified")
    return _openssl(body, passphrase, decrypt=True)


PROTECTED_PATHS = {
    "/",
    "/boot",
    "/etc",
    "/usr",
    "/var",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/dev",
    "/proc",
    "/sys",
    "/System",
    "/Library",
    "/Applications",
}
PROTECTED_WINDOWS = {"c:\\", "c:\\windows", "c:\\program files", "c:\\program files (x86)"}


def _tar_bytes(path: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        # Store the contents relative to the folder, so restoring into `name` gives `name/...`
        # rather than `name/name/...`.
        archive.add(str(path), arcname=".", recursive=True)
    return buffer.getvalue()


def _safe_members(archive: tarfile.TarFile, dest: Path) -> List[tarfile.TarInfo]:
    safe: List[tarfile.TarInfo] = []
    for member in archive.getmembers():
        name = member.name.replace("\\", "/")
        if not name or name.startswith("/") or ".." in Path(name).parts:
            continue
        if member.issym() or member.islnk() or member.isdev():
            continue
        if not (member.isfile() or member.isdir()):
            continue
        target = (dest / name).resolve()
        try:
            target.relative_to(dest.resolve())
        except ValueError:
            continue
        safe.append(member)
    return safe


def _untar(data: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = _safe_members(archive, dest)
        if hasattr(tarfile, "data_filter"):
            archive.extractall(dest, members=members, filter="data")
        else:
            for member in members:
                archive.extract(member, dest)


def guard(path: Path, workspace: Optional[Path] = None) -> Path:
    """Refuse the paths where encryption would be catastrophic or meaningless."""
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise VaultError(f"no such file or directory: {path}") from None
    if path.is_symlink():
        raise VaultError("refusing a symlink; encrypt the real file instead")
    text = str(resolved).lower().rstrip("\\/") or "/"
    if str(resolved) in PROTECTED_PATHS or text in PROTECTED_PATHS or text in PROTECTED_WINDOWS:
        raise VaultError(f"refusing to encrypt a system location: {resolved}")
    if resolved == Path.home().resolve():
        raise VaultError("refusing to encrypt your entire home directory; choose a folder instead")
    if (resolved / ".offgrid").exists() or (resolved / ".git").exists():
        if resolved == path.resolve():
            raise VaultError("refusing to encrypt a project root: it contains the project's own state")
    if workspace:
        try:
            workspace_resolved = workspace.resolve()
        except OSError:
            workspace_resolved = workspace
        if (
            resolved == workspace_resolved
            or workspace_resolved in resolved.parents
            or resolved in workspace_resolved.parents
        ):
            raise VaultError(
                "refusing to encrypt the workspace, anything inside it, or a parent of it: "
                "the agents would lose their files mid-run"
            )
    parts = {part.lower() for part in resolved.parts}
    if parts & {".git", ".offgrid"}:
        raise VaultError("refusing to encrypt version-control or OffGrid state directories")
    return resolved


def encrypt_path(path: Path, passphrase: str, workspace: Optional[Path] = None) -> Path:
    resolved = guard(Path(path), workspace)
    if len(passphrase) < MIN_PASSPHRASE:
        raise VaultError(f"use a passphrase of at least {MIN_PASSPHRASE} characters")
    output = resolved.with_name(resolved.name + ".ogv")
    if output.exists():
        raise VaultError(f"{output} already exists; remove it or choose another name")
    if resolved.is_dir():
        payload = _tar_bytes(resolved)
    else:
        payload = resolved.read_bytes()
    blob = seal(payload, passphrase)
    output.write_bytes(blob)
    try:
        output.chmod(0o600)
    except OSError:
        pass
    return output


def decrypt_path(path: Path, passphrase: str, out: Optional[Path] = None) -> Path:
    source = Path(path)
    if not source.is_file():
        raise VaultError(f"no such vault file: {source}")
    plain = unseal(source.read_bytes(), passphrase)
    stem = source.name[:-4] if source.name.endswith(".ogv") else source.name + ".restored"
    destination = Path(out) if out else source.with_name(stem)
    if destination.exists():
        raise VaultError(f"{destination} already exists; pass an explicit --out path")
    if plain[:2] == b"\x1f\x8b":
        _untar(plain, destination)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(plain)
        try:
            destination.chmod(0o600)
        except OSError:
            pass
    return destination


def plan() -> str:
    system = platform.system()
    lines = [
        "FULL-DISK ENCRYPTION - THE HONEST ANSWER",
        "",
        "OffGrid will not encrypt the running system for you. Full-disk encryption happens",
        "before the OS boots, through the platform's own tool. Doing it from inside a running",
        "session can leave the machine unbootable with no way back, and a coding agent should",
        "not be able to do that by accident.",
        "",
        "Use the platform's supported route, after a full backup:",
    ]
    if system == "Windows":
        lines += [
            "  Windows  BitLocker: Settings > Privacy & security > Device encryption, or",
            "           `manage-bde -on C:`. Save the recovery key somewhere that is not this disk.",
        ]
    elif system == "Darwin":
        lines += [
            "  macOS    FileVault: System Settings > Privacy & Security > FileVault. Save the",
            "           recovery key somewhere that is not this disk.",
        ]
    else:
        lines += [
            "  Linux    LUKS: `cryptsetup luksFormat /dev/sdXN`, then open it with cryptsetup and",
            "           put the filesystem inside. This erases the partition: back it up first.",
        ]
    lines += [
        "",
        "What OffGrid can do safely, now:",
        "  offgrid security encrypt PATH    encrypt one file or folder into PATH.ogv",
        "  offgrid security decrypt FILE    restore it with the same passphrase",
        "",
        "Encrypting a file does not erase the plaintext. On SSDs and copy-on-write filesystems",
        "there is no reliable in-place shred; if the plaintext must be gone, overwrite the disk",
        "with the platform's tool or destroy it.",
    ]
    return "\n".join(lines)
