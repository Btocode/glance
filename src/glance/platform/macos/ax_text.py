"""UTF-16 offsets (what AX ranges count) to and from Python `str` indices. Pure.

`AXSelectedTextRange`, `AXBoundsForRange` and every `CFRange` the Accessibility API uses
count UTF-16 code units; `FieldRead` offsets are Python indices (code points). A character
outside the BMP is two units there and one index here.
"""
from __future__ import annotations


def _units(ch: str) -> int:
    return 2 if ord(ch) > 0xFFFF else 1


def utf16_len(text: str) -> int:
    return sum(_units(ch) for ch in text)


def utf16_to_py(text: str, offset: int) -> int:
    """The index into `text` at UTF-16 `offset`; an offset inside a surrogate pair goes to
    the end of its character. Clamped to [0, len(text)]."""
    if offset <= 0:
        return 0
    units = 0
    for i, ch in enumerate(text):
        if units >= offset:
            return i
        units += _units(ch)
    return len(text)


def py_to_utf16(text: str, index: int) -> int:
    """The UTF-16 offset of index `index` into `text`, clamped to [0, utf16_len(text)]."""
    index = max(0, min(index, len(text)))
    return utf16_len(text[:index])


def range_to_py(text: str, loc: int, length: int) -> tuple[int, int]:
    """A `CFRange` (UTF-16) as a Python [start, end)."""
    start = utf16_to_py(text, loc)
    end = utf16_to_py(text, loc + max(0, length))
    return start, max(start, end)


def range_to_utf16(text: str, start: int, end: int) -> tuple[int, int]:
    """A Python [start, end) as a `CFRange` (location, length) in UTF-16 units."""
    a = py_to_utf16(text, start)
    b = py_to_utf16(text, max(start, end))
    return a, b - a
