"""Image sizes from headers, for each format an archive holds."""

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from desync_archival.services.image_size import size_of


def png(width, height):
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + header
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(header)) + chunk + struct.pack(">I", zlib.crc32(chunk))


def jpeg(width, height, padding=0):
    # An APP1 block of the given size before the frame, like a camera's EXIF.
    app1 = b""
    while padding > 0:
        part = min(padding, 65000)
        app1 += b"\xff\xe1" + struct.pack(">H", part + 2) + b"\x00" * part
        padding -= part
    frame = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x00" * 9
    return b"\xff\xd8" + app1 + frame + b"\xff\xd9"


class ImageSizeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def size(self, data, name="x"):
        path = self.dir / name
        path.write_bytes(data)
        return size_of(path)

    def test_png(self):
        self.assertEqual(self.size(png(1200, 800)), (1200, 800))

    def test_jpeg(self):
        self.assertEqual(self.size(jpeg(2000, 1500)), (2000, 1500))

    def test_a_jpeg_whose_size_sits_behind_large_metadata(self):
        # Two real previews put theirs 160 KB in, behind EXIF and XMP blocks.
        self.assertEqual(self.size(jpeg(640, 480, padding=170_000)), (640, 480))

    def test_gif(self):
        self.assertEqual(self.size(b"GIF89a" + struct.pack("<HH", 320, 240) + b"\x00" * 10), (320, 240))

    def test_webp_lossy(self):
        body = b"VP8 " + b"\x00" * 10 + struct.pack("<HH", 512, 256)
        self.assertEqual(self.size(b"RIFF" + b"\x00" * 4 + b"WEBP" + body), (512, 256))

    def test_webp_extended(self):
        body = b"VP8X" + b"\x00" * 8 + (1023).to_bytes(3, "little") + (767).to_bytes(3, "little")
        self.assertEqual(self.size(b"RIFF" + b"\x00" * 4 + b"WEBP" + body), (1024, 768))

    def test_anything_else_is_none(self):
        self.assertIsNone(self.size(b"not an image"))
        self.assertIsNone(self.size(b"\xff\xd8\xff\xe1\x00"))
        self.assertIsNone(size_of(self.dir / "missing.png"))
