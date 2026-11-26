"""Placement: caret -> previous char -> field -> window, clamped to the anchor's monitor.

`place` gives the controller its anchor point; `place_strip` puts the suggestion strip and its
drawer, `place_status` the status icon.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from glance.protocol import Rect

_MAX_CARET_H = 200


def _centre(r: Rect) -> tuple[float, float]:
    return r.x + r.w / 2, r.y + r.h / 2


def _monitor_at(r: Rect, monitors: list[Rect]) -> Rect | None:
    cx, cy = _centre(r)
    for m in monitors:
        if m.x <= cx < m.x + m.w and m.y <= cy < m.y + m.h:
            return m
    return None


def _usable(r: Rect | None, monitors: list[Rect], *, caret_like: bool) -> Rect | None:
    """Return the monitor holding r's centre when r is a plausible anchor, else None."""
    if r is None or r.w < 0 or r.h <= 0:
        return None
    if caret_like and r.h > _MAX_CARET_H:
        return None
    return _monitor_at(r, monitors)


def _clamp(x: int, y: int, size: tuple[int, int], mon: Rect) -> tuple[int, int]:
    w, h = size
    x = max(mon.x, min(x, mon.x + mon.w - w))
    y = max(mon.y, min(y, mon.y + mon.h - h))
    return x, y


def place(
    caret: Rect | None,
    prev_char: Rect | None,
    field: Rect | None,
    window: Rect | None,
    size: tuple[int, int],
    monitors: list[Rect],
    offset: int = 4,
) -> tuple[int, int] | None:
    """Top-left for a chip of `size`, or None when no anchor is usable."""
    mon = _usable(caret, monitors, caret_like=True)
    if mon is not None and caret is not None:
        return _clamp(caret.x + caret.w + offset, caret.y + caret.h + offset, size, mon)
    mon = _usable(prev_char, monitors, caret_like=True)
    if mon is not None and prev_char is not None:
        return _clamp(prev_char.x + prev_char.w + offset, prev_char.y + prev_char.h + offset, size, mon)
    for box in (field, window):
        mon = _usable(box, monitors, caret_like=False)
        if mon is not None and box is not None:
            return _clamp(box.x + box.w - size[0] - offset, box.y + box.h - size[1] - offset, size, mon)
    return None


def clamp_to_monitor(x: int, y: int, size: tuple[int, int],
                     monitors: list[Rect]) -> tuple[int, int]:
    """Top-left for a window of `size` wanted at (x, y), kept whole on the monitor under it.

    A point on no monitor uses the nearest one; without monitors the point is returned as is.
    """
    if not monitors:
        return x, y

    def distance(m: Rect) -> int:
        dx = max(m.x - x, 0, x - (m.x + m.w - 1))
        dy = max(m.y - y, 0, y - (m.y + m.h - 1))
        return dx + dy

    return _clamp(x, y, size, min(monitors, key=distance))


# ---------------------------------------------------------------------- the strip

STRIP_MIN_W = 320
STRIP_MAX_W = 720


@dataclass(frozen=True)
class Anchor:
    """Where the field is on screen: what the strip and its drawer are placed by. `pid` is
    the process of the window the field is in (the window the overlay follows)."""

    caret: Rect | None = None
    prev_char: Rect | None = None
    field: Rect | None = None
    window: Rect | None = None
    pid: int = 0
    tail: bool = False  # text follows the caret on its line: the line may reach the right edge

    def moved(self, dx: int, dy: int) -> Anchor:
        def shift(r: Rect | None) -> Rect | None:
            return None if r is None else Rect(r.x + dx, r.y + dy, r.w, r.h)

        return replace(self, caret=shift(self.caret), prev_char=shift(self.prev_char),
                       field=shift(self.field), window=shift(self.window))


@dataclass(frozen=True)
class Placed:
    x: int
    y: int
    w: int
    above: bool  # flipped above the line (or the field): no room below it


def place_strip(anchor: Anchor, height: int, monitors: list[Rect],
                gap: int = 6) -> Placed | None:
    """Top-left and width for a strip `height` px tall, or None without a usable anchor.

    Left-aligned with the field (the window when the field is unknown), as wide as it within
    STRIP_MIN_W..STRIP_MAX_W and the monitor, just below the caret line (previous character,
    then the field's bottom). With no room below it goes above that line instead: it never
    covers the line being typed.
    """
    line = None
    mon = None
    for r in (anchor.caret, anchor.prev_char):
        mon = _usable(r, monitors, caret_like=True)
        if mon is not None:
            line = r
            break
    box = None
    for r in (anchor.field, anchor.window):
        m = _usable(r, monitors, caret_like=False)
        if m is not None and r is not None and r.w > 0:
            box = r
            mon = mon or m
            break
    ref = line or box
    if ref is None or mon is None:
        return None
    width = min(max(box.w if box is not None else STRIP_MIN_W, STRIP_MIN_W), STRIP_MAX_W,
                mon.w)
    x = box.x if box is not None else ref.x
    if line is not None and box is not None and not box.x <= line.x <= box.x + box.w:
        x = line.x  # the field rect is stale or wrong: stay with the caret
    x = max(mon.x, min(x, mon.x + mon.w - width))
    y = ref.y + ref.h + gap
    above = y + height > mon.y + mon.h
    if above:
        y = ref.y - gap - height
    y = max(mon.y, min(y, mon.y + mon.h - height))
    return Placed(x, y, width, above)


# ---------------------------------------------------------------------- the status icon


def place_status(anchor: Anchor, size: int, monitors: list[Rect],
                 gap: int = 4) -> tuple[int, int] | None:
    """Top-left of the `size` px status icon, or None without a usable anchor.

    Vertically centred on the caret line (previous character, else the field's first line);
    horizontally at the right end of that line, inside the field's right edge. It never covers
    the text being typed: when the caret is near that edge or text follows it on its line, the
    icon goes just outside the field's right edge, and when the monitor has no room there,
    above the line.
    """
    line = None
    mon = None
    for r in (anchor.caret, anchor.prev_char):
        mon = _usable(r, monitors, caret_like=True)
        if mon is not None:
            line = r
            break
    box = None
    for r in (anchor.field, anchor.window):
        m = _usable(r, monitors, caret_like=False)
        if m is not None and r is not None and r.w > 0:
            box = r
            mon = mon or m
            break
    if mon is None or (line is None and box is None):
        return None
    if line is not None and box is not None and not box.x <= line.x <= box.x + box.w:
        box = None  # the field rect is stale or wrong: stay with the caret
    if line is not None:
        y = round(line.y + line.h / 2 - size / 2)
    else:
        y = box.y + gap  # type: ignore[union-attr]
    if box is None:
        x = line.x + line.w + 2 * gap  # type: ignore[union-attr]
    else:
        right = box.x + box.w
        inside = right - size - gap
        clear = (line is not None and not anchor.tail
                 and line.x + line.w + 3 * gap <= inside)
        if clear:
            x = inside
        elif right + gap + size <= mon.x + mon.w:
            x = right + gap
        else:
            x = inside
            ref = line or box
            y = ref.y - size - gap  # type: ignore[union-attr]
    return _clamp(x, y, (size, size), mon)
