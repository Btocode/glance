"""Coordinate spaces on macOS. Pure: no AppKit here.

Glance's shared code and the wire protocol use global top-left coordinates, y down (as X11
does). The Accessibility API uses that same space: its origin is the top-left corner of the
primary display. AppKit places windows in bottom-left coordinates, y up, whose origin is the
bottom-left corner of the primary display. Only the overlay converts, with these functions.
"""
from __future__ import annotations

from collections.abc import Iterable

from glance.protocol import Rect


def to_cocoa(r: Rect, primary_h: float) -> tuple[float, float, float, float]:
    """A top-left `Rect` as an AppKit frame (x, y, w, h) with its origin at the bottom-left."""
    return (float(r.x), float(primary_h - (r.y + r.h)), float(r.w), float(r.h))


def from_cocoa(x: float, y: float, w: float, h: float, primary_h: float) -> Rect:
    """An AppKit frame (bottom-left origin) as a top-left `Rect`, rounded to whole pixels."""
    return Rect(round(x), round(primary_h - (y + h)), round(w), round(h))


def point_from_cocoa(x: float, y: float, primary_h: float) -> tuple[int, int]:
    """A point in AppKit space (e.g. `NSEvent.mouseLocation`) in top-left space."""
    return round(x), round(primary_h - y)


def ax_rect(pos: tuple[float, float] | None, size: tuple[float, float] | None) -> Rect | None:
    """`AXPosition` + `AXSize` (already top-left) as a `Rect`; None when either is missing or
    the size is not positive in both directions."""
    if pos is None or size is None:
        return None
    w, h = size
    if w <= 0 or h <= 0:
        return None
    return Rect(round(pos[0]), round(pos[1]), round(w), round(h))


def caret_from_bounds(bounds: tuple[float, float, float, float] | None,
                      screens: Iterable[Rect] = ()) -> Rect | None:
    """`AXBoundsForRange` of a one-character range as a caret-like `Rect`.

    Apps answer an empty or off-screen rect for a range they cannot place (a zero rect at the
    origin from NSTextView for an empty line in some versions); those are None. A zero width
    is widened to 1 px: a caret has no width, but placement needs one.
    """
    if bounds is None:
        return None
    x, y, w, h = bounds
    if h <= 0 or (x == 0 and y == 0 and w == 0):
        return None
    r = Rect(round(x), round(y), max(1, round(w)), round(h))
    screens = list(screens)
    if screens and not any(s.x <= r.x < s.x + s.w and s.y <= r.y < s.y + s.h for s in screens):
        return None
    return r
