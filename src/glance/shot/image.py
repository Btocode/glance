"""A captured image, held in memory as PNG, and the few pixel operations Shot needs.

Pure Python (zlib): the capture backends hand over raw pixels (X11's BGRX) or a PNG (the
GNOME extension, macOS), and everything after that works on PNG bytes. No file is involved.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from glance.protocol import Rect

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class Shot:
    """One capture: the PNG, its pixel size, and the screen rectangle it was taken from
    (logical coordinates, for placing the bar)."""

    png: bytes
    width: int
    height: int
    rect: Rect

    def __repr__(self) -> str:  # never the pixels in a log or a traceback
        return f"Shot({self.width}x{self.height} at {self.rect.x},{self.rect.y})"


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def encode_png(pixels: bytes, width: int, height: int, channels: int = 3) -> bytes:
    """8-bit grey (1), RGB (3) or RGBA (4) pixels, rows top to bottom, as a PNG."""
    colour = {1: 0, 3: 2, 4: 6}.get(channels)
    if colour is None:
        raise ValueError(f"unsupported channel count: {channels}")
    stride = width * channels
    if width <= 0 or height <= 0 or len(pixels) != stride * height:
        raise ValueError("pixel data does not match the size")
    view = memoryview(pixels)
    raw = b"".join(b"\x00" + view[i * stride:(i + 1) * stride] for i in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0)
    return (PNG_SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(raw, 6))
            + _chunk(b"IEND", b""))


def png_size(png: bytes) -> tuple[int, int]:
    """(width, height) from a PNG's header; ValueError when it is not a PNG."""
    if len(png) < 24 or not png.startswith(PNG_SIGNATURE) or png[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    return struct.unpack(">II", png[16:24])


def bgrx_to_rgb(raw: bytes, width: int, height: int, stride: int | None = None) -> bytes:
    """X11 ZPixmap pixels (32 bits each, B G R X in memory) to packed RGB."""
    stride = stride or width * 4
    if stride == width * 4:
        packed = raw[:width * height * 4]
    else:
        packed = b"".join(raw[r * stride:r * stride + width * 4] for r in range(height))
    if len(packed) != width * height * 4:
        raise ValueError("pixel data does not match the size")
    out = bytearray(width * height * 3)
    out[0::3] = packed[2::4]
    out[1::3] = packed[1::4]
    out[2::3] = packed[0::4]
    return bytes(out)


def crop(pixels: bytes, width: int, channels: int, box: Rect) -> bytes:
    """The `box` (in pixels of the image) out of packed rows `width` pixels wide."""
    stride = width * channels
    row = box.w * channels
    start = box.x * channels
    return b"".join(pixels[(box.y + r) * stride + start:(box.y + r) * stride + start + row]
                    for r in range(box.h))


def scaled_rect(region: Rect, origin: Rect, scale: float) -> Rect:
    """`region` (logical screen coordinates) in the pixels of an image of `origin` taken at
    `scale` device pixels per logical pixel."""
    return Rect(round((region.x - origin.x) * scale), round((region.y - origin.y) * scale),
                max(1, round(region.w * scale)), max(1, round(region.h * scale)))
