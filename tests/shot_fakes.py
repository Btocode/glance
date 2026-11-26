"""Fakes shared by Glance Shot's tests."""
from __future__ import annotations

import struct
import zlib

from glance.backends.base import BaseBackend, Health
from glance.shot import ocr
from glance.shot.image import png_size


def decode_png(png: bytes) -> tuple[int, int, int, bytes]:
    """A test-only decoder for what encode_png writes (filter 0 rows)."""
    w, h = png_size(png)
    pos, idat, colour = 8, b"", None
    while pos < len(png):
        (n,), kind = struct.unpack(">I", png[pos:pos + 4]), png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + n]
        assert struct.unpack(">I", png[pos + 8 + n:pos + 12 + n])[0] == zlib.crc32(kind + data)
        if kind == b"IHDR":
            colour = data[9]
        if kind == b"IDAT":
            idat += data
        pos += 12 + n
    ch = {0: 1, 2: 3, 6: 4}[colour]
    raw = zlib.decompress(idat)
    rows = [raw[r * (w * ch + 1):(r + 1) * (w * ch + 1)] for r in range(h)]
    assert all(r[0] == 0 for r in rows)
    return w, h, ch, b"".join(r[1:] for r in rows)


class FakeEngine:
    def __init__(self, name, out=None, available=True, error=None):
        self.name, self.out, self._available, self.error = name, out, available, error

    def available(self):
        return self._available

    def needs_download(self):
        return False

    def text(self, png):
        if self.error:
            raise ocr.OcrUnavailable(self.error)
        return self.out


class VisionFake(BaseBackend):
    def __init__(self, name: str, vision: bool, out: str = "seen") -> None:
        self.name, self.vision, self.out = name, vision, out
        self.seen: list[bytes] = []

    def see(self, png, system, user):
        self.seen.append(png)
        yield self.out

    def answer(self, system, user):
        yield "answer"

    def cancel(self):
        pass

    def health(self):
        return Health(True, True, "ok")
