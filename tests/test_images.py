"""Image optimization: lossless PNG rebuilds, JPEG metadata removal, safe writes."""

import tempfile
import unittest
import zlib
from pathlib import Path

from offgrid.images import ImageError, compress_image


def chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return len(payload).to_bytes(4, "big") + kind + payload + crc.to_bytes(4, "big")


def make_png(level: int = 0) -> bytes:
    # 4x4 RGB, filter byte zero before each scanline. Level 0 is deliberately
    # wasteful so the optimizer has something real to remove.
    pixels = b"".join(b"\x00" + b"\xff\x00\x00" * 4 for _ in range(4))
    ihdr = (
        (4).to_bytes(4, "big")
        + (4).to_bytes(4, "big")
        + bytes((8, 2, 0, 0, 0))
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(pixels, level))
        + chunk(b"IEND", b"")
    )


def idat_bytes(data: bytes) -> bytes:
    position = 8
    parts = []
    while position < len(data):
        length = int.from_bytes(data[position : position + 4], "big")
        kind = data[position + 4 : position + 8]
        if kind == b"IDAT":
            parts.append(data[position + 8 : position + 8 + length])
        position += 12 + length
    return b"".join(parts)


def make_jpeg_with_metadata() -> bytes:
    payload = b"Exif\x00\x00orientation=6"
    app1 = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    sos = b"\xff\xda" + (2 + 6).to_bytes(2, "big") + b"\x00" * 6
    return b"\xff\xd8" + app1 + sos + b"\x00\x01" + b"\xff\xd9"


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_png_is_recompressed_losslessly(self):
        source = self.root / "sample.png"
        original = make_png()
        source.write_bytes(original)
        result = compress_image(source)
        self.assertEqual(result.status, "compressed")
        self.assertLess(result.after_bytes, result.before_bytes)
        self.assertEqual(result.output, self.root / "sample.compressed.png")
        self.assertEqual(source.read_bytes(), original)
        optimized = result.output.read_bytes()
        self.assertEqual(zlib.decompress(idat_bytes(optimized)), zlib.decompress(idat_bytes(original)))

    def test_png_in_place_is_atomic_and_leaves_no_copy(self):
        source = self.root / "sample.png"
        source.write_bytes(make_png())
        result = compress_image(source, in_place=True)
        self.assertEqual(result.output, source)
        self.assertLess(result.after_bytes, result.before_bytes)
        self.assertFalse((self.root / "sample.compressed.png").exists())

    def test_existing_destination_is_never_overwritten(self):
        source = self.root / "sample.png"
        source.write_bytes(make_png())
        destination = self.root / "already.png"
        destination.write_bytes(b"keep me")
        with self.assertRaises(FileExistsError):
            compress_image(source, destination=destination)
        self.assertEqual(destination.read_bytes(), b"keep me")

    def test_jpeg_metadata_can_be_stripped_without_reencoding_pixels(self):
        source = self.root / "photo.jpg"
        source.write_bytes(make_jpeg_with_metadata())
        result = compress_image(source, strip_metadata=True)
        self.assertEqual(result.status, "compressed")
        self.assertLess(result.after_bytes, result.before_bytes)
        optimized = result.output.read_bytes()
        self.assertNotIn(b"Exif", optimized)
        self.assertTrue(optimized.startswith(b"\xff\xd8"))
        self.assertTrue(optimized.endswith(b"\xff\xd9"))

    def test_jpeg_is_left_alone_without_metadata_opt_in(self):
        source = self.root / "photo.jpg"
        source.write_bytes(make_jpeg_with_metadata())
        result = compress_image(source)
        self.assertEqual(result.status, "already-optimal")
        self.assertIsNone(result.output)
        self.assertEqual(source.read_bytes(), make_jpeg_with_metadata())

    def test_invalid_images_are_refused(self):
        source = self.root / "broken.png"
        source.write_bytes(b"not an image")
        with self.assertRaises(ImageError) as caught:
            compress_image(source)
        self.assertIn("unsupported", str(caught.exception))

    def test_corrupt_png_checksum_is_refused(self):
        source = self.root / "broken.png"
        data = bytearray(make_png())
        data[20] ^= 0xFF
        source.write_bytes(data)
        with self.assertRaises(ImageError) as caught:
            compress_image(source)
        self.assertIn("checksum", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
