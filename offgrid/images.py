"""Small, dependency-free image compression helpers.

PNG is recompressed losslessly at the zlib level: pixels and ancillary chunks
stay the same, while IDAT data is consolidated and rebuilt. JPEG pixel data is
never re-encoded; the JPEG path can only remove APPn/COM metadata when the
caller explicitly asks for it. There is no Pillow or external image tool to
install.
"""

from __future__ import annotations

import os
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_PNG_DATA = 128 * 1024 * 1024
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


class ImageError(Exception):
    """An image was unsupported, corrupt, or unsafe to write."""


@dataclass(frozen=True)
class CompressionResult:
    source: Path
    output: Optional[Path]
    kind: str
    before_bytes: int
    after_bytes: int
    status: str
    detail: str = ""

    @property
    def saved_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": str(self.source),
            "output": str(self.output) if self.output else "",
            "kind": self.kind,
            "before_bytes": self.before_bytes,
            "after_bytes": self.after_bytes,
            "saved_bytes": self.saved_bytes,
            "status": self.status,
            "detail": self.detail,
        }

    def text(self) -> str:
        if self.status != "compressed":
            return f"{self.source}: {self.detail or self.status} ({self.before_bytes} bytes)"
        percent = (self.saved_bytes / self.before_bytes * 100) if self.before_bytes else 0.0
        return (
            f"{self.source} -> {self.output}: {self.before_bytes} -> {self.after_bytes} bytes "
            f"({percent:.1f}% smaller)"
        )


def compress_image(
    source: Path,
    destination: Optional[Path] = None,
    in_place: bool = False,
    strip_metadata: bool = False,
) -> CompressionResult:
    """Compress one PNG or strip metadata from one JPEG.

    ``in_place`` is the only way to replace the source; otherwise a smaller
    result is written next to it as ``name.compressed.ext``, or to
    ``destination`` when supplied. Existing destinations are never overwritten.
    """
    source = Path(source)
    if in_place and destination is not None:
        raise ImageError("choose either in_place or a destination, not both")
    if not source.is_file():
        raise ImageError(f"not a file: {source}")
    try:
        before = source.stat().st_size
    except OSError as exc:
        raise ImageError(f"cannot read {source}: {exc}") from None
    if before > MAX_INPUT_BYTES:
        raise ImageError(f"{source} is larger than the {MAX_INPUT_BYTES // (1024 * 1024)} MB limit")
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ImageError(f"cannot read {source}: {exc}") from None

    kind = _image_kind(raw)
    if kind == "png":
        optimized = _optimize_png(raw)
    elif kind == "jpeg" and strip_metadata:
        optimized = _strip_jpeg_metadata(raw)
    else:
        optimized = raw

    if len(optimized) >= before:
        detail = (
            "already at the smallest size this build can produce"
            if kind == "png"
            else "JPEG pixels were not re-encoded"
        )
        return CompressionResult(source, None, kind, before, len(optimized), "already-optimal", detail)

    if in_place:
        target = source
    elif destination is not None:
        target = Path(destination)
        if target.is_dir():
            target = target / source.name
        if target.exists():
            raise FileExistsError(f"destination already exists: {target}")
    else:
        target = source.with_name(f"{source.stem}.compressed{source.suffix}")
        if target.exists():
            raise FileExistsError(f"destination already exists: {target}")

    _write_atomic(target, optimized, source)
    return CompressionResult(source, target, kind, before, len(optimized), "compressed")


def _image_kind(raw: bytes) -> str:
    if raw.startswith(PNG_SIGNATURE):
        return "png"
    if raw.startswith(JPEG_SIGNATURE):
        return "jpeg"
    raise ImageError("unsupported image format (expected PNG or JPEG)")


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return (
        len(payload).to_bytes(4, "big")
        + kind
        + payload
        + crc.to_bytes(4, "big")
    )


def _png_chunks(raw: bytes):
    if not raw.startswith(PNG_SIGNATURE):
        raise ImageError("not a PNG file")
    position = len(PNG_SIGNATURE)
    chunks = []
    while position < len(raw):
        if position + 12 > len(raw):
            raise ImageError("truncated PNG chunk header")
        length = int.from_bytes(raw[position : position + 4], "big")
        if length > MAX_INPUT_BYTES:
            raise ImageError("PNG chunk is too large")
        end = position + 12 + length
        if end > len(raw):
            raise ImageError("truncated PNG chunk")
        kind = raw[position + 4 : position + 8]
        if not kind or not all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in kind):
            raise ImageError("invalid PNG chunk type")
        payload = raw[position + 8 : position + 8 + length]
        expected = int.from_bytes(raw[position + 8 + length : end], "big")
        if (zlib.crc32(kind + payload) & 0xFFFFFFFF) != expected:
            raise ImageError("PNG chunk checksum failed")
        chunks.append((kind, payload))
        position = end
        if kind == b"IEND":
            if position != len(raw):
                raise ImageError("bytes after PNG IEND chunk")
            break
    if not chunks or chunks[0][0] != b"IHDR" or chunks[-1][0] != b"IEND":
        raise ImageError("PNG is missing IHDR or IEND")
    return chunks


def _optimize_png(raw: bytes) -> bytes:
    chunks = _png_chunks(raw)
    idat = [payload for kind, payload in chunks if kind == b"IDAT"]
    if not idat:
        raise ImageError("PNG has no IDAT data")
    compressed = b"".join(idat)
    try:
        decompressor = zlib.decompressobj()
        pixels = decompressor.decompress(compressed, MAX_PNG_DATA + 1)
        if len(pixels) > MAX_PNG_DATA or decompressor.unconsumed_tail:
            raise ImageError("PNG decompresses beyond the safety limit")
        pixels += decompressor.flush()
    except zlib.error:
        raise ImageError("PNG image data is invalid") from None
    if len(pixels) > MAX_PNG_DATA:
        raise ImageError("PNG decompresses beyond the safety limit")
    recompressed = zlib.compress(pixels, 9)
    if len(recompressed) >= len(compressed):
        return raw

    result = bytearray(PNG_SIGNATURE)
    inserted = False
    for kind, payload in chunks:
        if kind == b"IDAT":
            if not inserted:
                result.extend(_chunk(b"IDAT", recompressed))
                inserted = True
            continue
        result.extend(_chunk(kind, payload))
    return bytes(result)


def _strip_jpeg_metadata(raw: bytes) -> bytes:
    """Remove APPn/COM markers before the scan; never touch pixel entropy."""
    if not raw.startswith(b"\xff\xd8"):
        raise ImageError("not a JPEG file")
    result = bytearray(raw[:2])
    position = 2
    while position < len(raw):
        if raw[position] != 0xFF:
            raise ImageError("malformed JPEG marker")
        start = position
        while position < len(raw) and raw[position] == 0xFF:
            position += 1
        if position >= len(raw):
            raise ImageError("truncated JPEG marker")
        marker = raw[position]
        position += 1
        if marker == 0xD9:  # EOI
            result.extend(raw[start:position])
            result.extend(raw[position:])
            return bytes(result)
        if marker == 0xD8:
            raise ImageError("unexpected JPEG start marker")
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:  # standalone markers
            result.extend(raw[start:position])
            continue
        if position + 2 > len(raw):
            raise ImageError("truncated JPEG segment")
        length = int.from_bytes(raw[position : position + 2], "big")
        if length < 2:
            raise ImageError("invalid JPEG segment length")
        end = position + length
        if end > len(raw):
            raise ImageError("truncated JPEG segment")
        if marker == 0xDA:  # SOS; copy scan data and stop parsing headers
            result.extend(raw[start:end])
            result.extend(raw[end:])
            return bytes(result)
        if 0xE0 <= marker <= 0xEF or marker == 0xFE:
            position = end
            continue
        result.extend(raw[start:end])
        position = end
    raise ImageError("JPEG has no end marker")


def _write_atomic(target: Path, data: bytes, source: Path) -> None:
    target = Path(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ImageError(f"cannot create {target.parent}: {exc}") from None
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
        try:
            os.chmod(temporary, source.stat().st_mode & 0o777)
        except OSError:
            pass
        temporary.replace(target)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise ImageError(f"cannot write {target}: {exc}") from None
